#!/usr/bin/env python3
"""Live readout of /arm/joy in SDL slot names, for checking the panel mapping."""
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Joy

BTN = {0: 'A  safe_pose (home+servo)', 1: 'B  sampling_home', 2: 'X  astrobio_home  [rover: EXIT]',
       3: 'Y  drill_home', 4: 'BACK (unbound)', 5: 'GUIDE (unbound)', 6: 'START (not offered)',
       7: 'L3', 8: 'R3', 9: 'LB push_boost', 10: 'RB shift', 11: 'DPad-Up gripper_open',
       12: 'DPad-Down  [rover: panel_align]', 13: 'DPad-Left gripper_close', 14: 'DPad-Right'}
AX = {0: 'left_x  move L/R', 1: 'left_y  move fwd/back', 2: 'right_x yaw (roll w/ shift)',
      3: 'right_y up/down (pitch w/ shift)', 4: 'L2 (unused)', 5: 'R2 (unused)'}


class Watch(Node):
    def __init__(self):
        super().__init__('arm_joy_watch')
        self.pb, self.pa = None, None
        self.create_subscription(Joy, '/arm/joy', self.cb, 10)
        print('watching /arm/joy — press one control at a time\n')

    def cb(self, m):
        if self.pb is not None:
            for i, v in enumerate(m.buttons):
                if v != self.pb[i]:
                    print(f'button {i:2d}  {"PRESS " if v else "release"}  {BTN.get(i, "?")}')
        if self.pa is not None:
            for i, v in enumerate(m.axes):
                if abs(v - self.pa[i]) > 0.15:
                    print(f'axis   {i:2d}  {v:+.2f}          {AX.get(i, "?")}')
        self.pb, self.pa = list(m.buttons), list(m.axes)


def main():
    rclpy.init()
    try:
        rclpy.spin(Watch())
    except KeyboardInterrupt:
        pass


main()
