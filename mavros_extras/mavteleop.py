#!/usr/bin/env python
# vim:set ts=4 sw=4 et:
#
# Copyright 2014 Vladimir Ermakov.
#
# This file is part of the mavros package and subject to the license terms
# in the top-level LICENSE file of the mavros repository.
# https://github.com/mavlink/mavros/tree/master/LICENSE.md

from __future__ import print_function

import sys
import argparse
import rospy
import mavros

from tf.transformations import quaternion_from_euler
from sensor_msgs.msg import Joy
from std_msgs.msg import Header, Float64
from geometry_msgs.msg import PoseStamped, TwistStamped, Vector3, Quaternion, Point
from mavros_msgs.msg import OverrideRCIn
from mavros import command
from mavros import setpoint as SP


def arduino_map(x, inmin, inmax, outmin, outmax):
    return (x - inmin) * (outmax - outmin) / (inmax - inmin) + outmin


class RCChan(object):
    def __init__(self, name, chan, min_pos=-1.0):
        self.name = name
        self.chan = chan
        self.min = 1000
        self.max = 2000
        self.min_pos = min_pos

    def load_param(self):
        self.chan = rospy.get_param("~rc_map/" + self.name, self.chan)
        self.min = rospy.get_param("~rc_min/" + self.name, self.min)
        self.max = rospy.get_param("~rc_max/" + self.name, self.max)

    def calc_us(self, pos):
        # warn: limit check
        return arduino_map(pos, self.min_pos, 1.0, self.min, self.max)

class RCMode(object):
    def __init__( self, name, joy_flags, rc_channel, rc_value ):
        self.name = name
        self.joy_flags = joy_flags
        self.rc_channel = rc_channel
        self.rc_value = rc_value
        
    @staticmethod
    def load_param(ns='~rc_modes/'):
        yaml = rospy.get_param(ns)
        return [ RCMode( name, data['joy_flags'], data['rc_channel'], data['rc_value'] ) 
            for name,data in yaml.items() ]
        
    def is_toggled(self,joy):
        for btn,flag in self.joy_flags:
            if joy.buttons[btn] != flag:
                return False
        return True
        
    def apply_mode(self,joy,rc):
        if self.is_toggled(joy):
            rc.channels[self.rc_channel]=self.rc_value

# Mode 2 on Logitech F710 gamepad
axes_map = {
    'roll': 3,
    'pitch': 4,
    'yaw': 0,
    'throttle': 1
}

axes_scale = {
    'roll': 1.0,
    'pitch': 1.0,
    'yaw': 1.0,
    'throttle': 1.0
}

# XXX: todo
button_map = {
    'arm' : 0,
    'disarm' : 1,
    'takeoff': 2,
    'land': 3,
    'enable': 4
}


rc_channels = {
    'roll': RCChan('roll', 0),
    'pitch': RCChan('pitch', 1),
    'yaw': RCChan('yaw', 3),
    'throttle': RCChan('throttle', 2, 0.0)
}

def arm(args, state):
    try:
        command.arming(value=state)
    except rospy.ServiceException as ex:
        fault(ex)

    if not ret.success:
        rospy.loginfo("Request failed.")
    else:
        rospy.loginfo("Request success.")


def load_map(m, n):
    for k, v in m.items():
        m[k] = rospy.get_param(n + k, v)


def get_axis(j, n):
    return j.axes[axes_map[n]] * axes_scale[n]

def get_buttons(j, n):
    return j.buttons[ button_map[n]]

def rc_override_control(args):
    rospy.loginfo("MAV-Teleop: RC Override control type.")

    load_map(axes_map, '~axes_map/')
    load_map(axes_scale, '~axes_scale/')
    load_map(button_map, '~button_map/')
    for k, v in rc_channels.items():
        v.load_param()

    rc_modes = RCMode.load_param()
    rc = OverrideRCIn()

    override_pub = rospy.Publisher(mavros.get_topic("rc", "override"), OverrideRCIn, queue_size=10)

    def joy_cb(joy):
        # get axes normalized to -1.0..+1.0 RPY, 0.0..1.0 T
        roll = get_axis(joy, 'roll')
        pitch = get_axis(joy, 'pitch')
        yaw = get_axis(joy, 'yaw')
        throttle = arduino_map(get_axis(joy, 'throttle'), -1.0, 1.0, 0.0, 1.0)

        rospy.logdebug("RPYT: %f, %f, %f, %f", roll, pitch, yaw, throttle)

        def set_chan(n, v):
            ch = rc_channels[n]
            rc.channels[ch.chan] = ch.calc_us(v)
            rospy.logdebug("RC%d (%s): %d us", ch.chan, ch.name, ch.calc_us(v))


        set_chan('roll', roll)
        set_chan('pitch', pitch)
        set_chan('yaw', yaw)
        set_chan('throttle', throttle)
        
        for m in rc_modes:
            m.apply_mode(joy,rc)
        
        override_pub.publish(rc)


    jsub = rospy.Subscriber("joy", Joy, joy_cb)
    rospy.spin()


def attitude_setpoint_control(args):
    rospy.loginfo("MAV-Teleop: Attitude setpoint control type.")

    load_map(axes_map, '~axes_map/')
    load_map(axes_scale, '~axes_scale/')
    load_map(button_map, '~button_map/')

    att_pub = SP.get_pub_attitude_pose(queue_size=10)
    thd_pub = SP.get_pub_attitude_throttle(queue_size=10)

    if rospy.get_param(mavros.get_topic("setpoint_attitude", "reverse_throttle"), False):
        def thd_normalize(v):
            return v
    else:
        def thd_normalize(v):
            return arduino_map(v, -1.0, 1.0, 0.0, 1.0)

    def joy_cb(joy):
        # get axes normalized to -1.0..+1.0 RPY, 0.0..1.0 T
        roll = get_axis(joy, 'roll')
        pitch = get_axis(joy, 'pitch')
        yaw = get_axis(joy, 'yaw')
        throttle = thd_normalize(get_axis(joy, 'throttle'))

        rospy.logdebug("RPYT: %f, %f, %f, %f", roll, pitch, yaw, throttle)

        if get_buttons(joy,'arm') == 1:
            arm(args, True)
        elif get_buttons(joy,'disarm') == 1:
            arm(args, False)

        # TODO: Twist variation
        pose = PoseStamped(header=Header(stamp=rospy.get_rostime()))
        q = quaternion_from_euler(roll, pitch, yaw)
        pose.pose.orientation = Quaternion(*q)

        att_pub.publish(pose)
        thd_pub.publish(data=throttle)


    jsub = rospy.Subscriber("joy", Joy, joy_cb)
    rospy.spin()


def velocity_setpoint_control(args):
    rospy.loginfo("MAV-Teleop: Velocity setpoint control type.")

    load_map(axes_map, '~axes_map/')
    load_map(axes_scale, '~axes_scale/')
    load_map(button_map, '~button_map/')

    vel_pub = SP.get_pub_velocity_cmd_vel(queue_size=10)

    def joy_cb(joy):
        # get axes normalized to -1.0..+1.0 RPYT
        roll = get_axis(joy, 'roll')
        pitch = get_axis(joy, 'pitch')
        yaw = get_axis(joy, 'yaw')
        throttle = get_axis(joy, 'throttle')

        rospy.logdebug("RPYT: %f, %f, %f, %f", roll, pitch, yaw, throttle)

        # Based on QGC UAS joystickinput_settargets branch
        # not shure that it really need inegrating, as it done in QGC.
        twist = TwistStamped(header=Header(stamp=rospy.get_rostime()))
        twist.twist.linear = Vector3(x=roll, y=pitch, z=throttle)
        twist.twist.angular = Vector3(z=yaw)

        vel_pub.publish(twist)


    jsub = rospy.Subscriber("joy", Joy, joy_cb)
    rospy.spin()


px, py, pz = 0.0, 0.0, 0.0

def position_setpoint_control(args):
    rospy.loginfo("MAV-Teleop: Position setpoint control type.")

    load_map(axes_map, '~axes_map/')
    load_map(axes_scale, '~axes_scale/')
    load_map(button_map, '~button_map/')

    pos_pub = SP.get_pub_position_local(queue_size=10)

    def joy_cb(joy):
        global px, py, pz
        # get axes normalized to -1.0..+1.0 RPY
        roll = get_axis(joy, 'roll')
        pitch = get_axis(joy, 'pitch')
        yaw = get_axis(joy, 'yaw')
        throttle = get_axis(joy, 'throttle')

        # TODO: need integrate by time, joy_cb() called with variable frequency
        px -= pitch
        py += roll
        pz += throttle

        rospy.logdebug("RPYT: %f, %f, %f, %f", roll, pitch, yaw, throttle)
        rospy.logdebug("Point(%f %f %f)", px, py, pz)

        # Based on QGC UAS joystickinput_settargets branch
        pose = PoseStamped(header=Header(stamp=rospy.get_rostime()))
        pose.pose.position = Point(x=px, y=py, z=pz)
        q = quaternion_from_euler(0, 0, yaw)
        pose.pose.orientation = Quaternion(*q)

        pos_pub.publish(pose)


    jsub = rospy.Subscriber("joy", Joy, joy_cb)
    rospy.spin()


def main():
    parser = argparse.ArgumentParser(description="Teleoperation script for Copter-UAV")
    parser.add_argument('-n', '--mavros-ns', help="ROS node namespace", default=mavros.DEFAULT_NAMESPACE)
    parser.add_argument('-v', '--verbose', action='store_true', help="verbose output")
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument('-rc', '--rc-override', action='store_true', help="use rc override control type")
    mode_group.add_argument('-att', '--sp-attitude', action='store_true', help="use attitude setpoint control type")
    mode_group.add_argument('-vel', '--sp-velocity', action='store_true', help="use velocity setpoint control type")
    mode_group.add_argument('-pos', '--sp-position', action='store_true', help="use position setpoint control type")

    args = parser.parse_args(rospy.myargv(argv=sys.argv)[1:])

    rospy.init_node("mavteleop")
    mavros.set_namespace(args.mavros_ns)

    if args.rc_override:
        rc_override_control(args)
    elif args.sp_attitude:
        attitude_setpoint_control(args)
    elif args.sp_velocity:
        velocity_setpoint_control(args)
    elif args.sp_position:
        position_setpoint_control(args)


if __name__ == '__main__':
    main()
