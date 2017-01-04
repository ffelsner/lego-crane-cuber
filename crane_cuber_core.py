#!/usr/bin/env python3

"""
CraneCuber
A Rubiks cube solving robot made from EV3 + 42009
"""

from ev3dev.ev3 import OUTPUT_A, OUTPUT_B, OUTPUT_C, TouchSensor, LargeMotor, MediumMotor, Leds
from math import pi
from pprint import pformat
from rubikscolorresolver import RubiksColorSolver2x2x2, RubiksColorSolver3x3x3
from time import sleep
import datetime
import json
import logging
import os
import signal
import subprocess
import sys
import time

log = logging.getLogger(__name__)

# http://www.thegeekstuff.com/2008/11/3-steps-to-perform-ssh-login-without-password-using-ssh-keygen-ssh-copy-id
SERVER = '192.168.0.4'

# negative moves to init position
# positive moves towards camera
# This should be 90 degrees but some extra is needed to account for play between the gears
FLIPPER_DEGREES = 120
FLIPPER_SPEED = 600

# The gear ratio is 1:2.333
# The follower gear rotates 0.428633 time per each revolution of the driver gear
# We need the follower gear to rotate 90 degrees so 90/0.428633 = 209.96
#
# negative moves counter clockwise (viewed from above)
# positive moves clockwise (viewed from above)
TURNTABLE_TURN_DEGREES = 210
TURNTABLE_SPEED = 400

TURN_FREE_TOUCH_DEGREES = 40
TURN_FREE_SQUARE_TT_DEGREES = -40

# negative moves down
# positive moves up
ELEVATOR_SPEED_UP_FAST = 1050
ELEVATOR_SPEED_UP_SLOW = 800
ELEVATOR_SPEED_DOWN_FAST = 1050
ELEVATOR_SPEED_DOWN_SLOW = 200

# References
# ==========
# cube sizes
# http://cubeman.org/measure.txt
#
# README editing
# https://jbt.github.io/markdown-editor/


class CraneCuber3x3x3(object):

    def __init__(self, rows_and_cols=3, size_mm=57):
        Leds.set_color(Leds.LEFT, Leds.AMBER)
        Leds.set_color(Leds.RIGHT, Leds.AMBER)
        self.shutdown = False
        self.rows_and_cols = rows_and_cols
        self.size_mm = size_mm
        self.square_size_mm = float(self.size_mm / self.rows_and_cols)
        self.elevator = LargeMotor(OUTPUT_A)
        self.flipper = MediumMotor(OUTPUT_B)
        self.turntable= LargeMotor(OUTPUT_C)
        self.touch_sensor = TouchSensor()
        self.motors = [self.elevator, self.flipper, self.turntable]
        self.rows_in_turntable = 0
        self.facing_up = 'U'
        self.facing_down = 'D'
        self.facing_north = 'B'
        self.facing_west = 'L'
        self.facing_south = 'F'
        self.facing_east = 'R'
        signal.signal(signal.SIGTERM, self.signal_term_handler)
        signal.signal(signal.SIGINT, self.signal_int_handler)

        # These numbers are for a 57mm 3x3x3 cube
        self.TURN_BLOCKED_TOUCH_DEGREES = 105
        self.TURN_BLOCKED_SQUARE_CUBE_DEGREES = -145
        self.TURN_BLOCKED_SQUARE_TT_DEGREES = 40
        self.rows_in_turntable_to_count_as_face_turn = 2

        self.init_motors()

    def init_motors(self):

        for x in self.motors:
            if not x.connected:
                log.error("%s is not connected" % x)
                sys.exit(1)
            x.reset()

        # 'brake' stops but doesn't hold the motor in place
        # 'hold' stops and holds the motor in place
        log.info("Initialize elevator %s" % self.elevator)
        self.elevator.reset()
        self.elevator.stop(stop_action='brake')

        log.info("Initialize flipper %s" % self.flipper)
        self.flipper.run_forever(speed_sp=-60, stop_action='hold')
        self.flipper.wait_until('running')
        self.flipper.wait_until('stalled')
        self.flipper.stop()
        self.flipper.reset()
        self.flipper.stop(stop_action='hold')
        self.flipper_at_init = True

        log.info("Initialize turntable %s" % self.turntable)
        self.turntable.reset()
        self.turntable.stop(stop_action='hold')

    def shutdown_robot(self):
        log.info('Shutting down')
        self.elevate(0)

        flipper_pos = self.flipper.position

        if flipper_pos > 10:
            self.flip()

        self.shutdown = True

        for x in self.motors:
            x.shutdown = True

        for x in self.motors:
            x.stop(stop_action='brake')

    def signal_term_handler(self, signal, frame):
        log.error('Caught SIGTERM')
        self.shutdown_robot()

    def signal_int_handler(self, signal, frame):
        log.error('Caught SIGINT')
        self.shutdown_robot()

    def wait_for_touch_sensor(self):
        log.info('waiting for TouchSensor press')

        while True:
            if self.shutdown:
                break

            if self.touch_sensor.is_pressed:
                log.info('TouchSensor pressed')
                break

            sleep(0.01)

    def _rotate(self, final_pos):
        self.turntable.run_to_abs_pos(position_sp=final_pos,
                                      speed_sp=TURNTABLE_SPEED,
                                      stop_action='hold',
                                      ramp_up_sp=0)
        self.turntable.wait_while('running')
        prev_pos = None

        while self.turntable.position != final_pos:
            log.info("turntable() position is %d, it should be %d" % (self.turntable.position, final_pos))
            self.turntable.run_to_abs_pos(position_sp=final_pos,
                                         speed_sp=50)
            self.turntable.wait_while('running', timeout=100)

            if prev_pos is not None and self.turntable.position == prev_pos:
                break
            prev_pos = self.turntable.position

    def rotate(self, clockwise, quarter_turns):

        if self.shutdown:
            return

        assert quarter_turns > 0 and quarter_turns <= 2, "quarter_turns is %d, it must be between 0 and 2" % quarter_turns
        current_pos = self.turntable.position

        # cube will turn freely since none of the rows are being held
        if self.rows_in_turntable == self.rows_and_cols:
            turn_degrees = TURN_FREE_TOUCH_DEGREES + (TURNTABLE_TURN_DEGREES * quarter_turns)
            square_turntable_degrees = TURN_FREE_SQUARE_TT_DEGREES

            if not clockwise:
                turn_degrees *= -1
                square_turntable_degrees *= -1

            turn_pos = current_pos + turn_degrees
            square_turntable_pos = turn_pos + square_turntable_degrees

            log.info("rotate_cube() %d quarter turns, clockwise %s, current_pos %d, turn_pos %d, square_turntable_pos %d" %
                (quarter_turns, clockwise, current_pos, turn_pos, square_turntable_pos))

            self._rotate(turn_pos)
            self._rotate(square_turntable_pos)

        else:
            turn_degrees = self.TURN_BLOCKED_TOUCH_DEGREES + (TURNTABLE_TURN_DEGREES * quarter_turns)
            square_cube_degrees = self.TURN_BLOCKED_SQUARE_CUBE_DEGREES
            square_turntable_degrees = self.TURN_BLOCKED_SQUARE_TT_DEGREES

            if not clockwise:
                turn_degrees *= -1
                square_cube_degrees *= -1
                square_turntable_degrees *= -1

            turn_pos = current_pos + turn_degrees
            square_cube_pos = turn_pos + square_cube_degrees
            square_turntable_pos = square_cube_pos + square_turntable_degrees
            log.info("rotate_cube() %d quarter turns, clockwise %s, current_pos %d, turn_pos %d, square_cube_pos %d, square_turntable_pos %d" %
                (quarter_turns, clockwise, current_pos, turn_pos, square_cube_pos, square_turntable_pos))

            self._rotate(turn_pos)
            self._rotate(square_cube_pos)
            self._rotate(square_turntable_pos)

        # Only update the facing_XYZ variables if the entire side is turning.  For
        # a 3x3x3 this means the middle square is being turned, this happens if at
        # least two rows are up in the turntable
        if self.rows_in_turntable >= self.rows_in_turntable_to_count_as_face_turn:
            orig_north = self.facing_north
            orig_west = self.facing_west
            orig_south = self.facing_south
            orig_east = self.facing_east

            if quarter_turns == 2:
                self.facing_north = orig_south
                self.facing_west = orig_east
                self.facing_south = orig_north
                self.facing_east = orig_west
            else:
                if clockwise:
                    self.facing_north = orig_west
                    self.facing_west = orig_south
                    self.facing_south = orig_east
                    self.facing_east = orig_north
                else:
                    self.facing_north = orig_east
                    self.facing_west = orig_north
                    self.facing_south = orig_west
                    self.facing_east = orig_south

            # log.info("north %s, west %s, south %s, east %s (original)" % (orig_north, orig_west, orig_south, orig_east))
            # log.info("north %s, west %s, south %s, east %s" % (self.facing_north, self.facing_west, self.facing_south, self.facing_east))

    def flip_settle_cube(self):

        if self.shutdown:
            return

        self.flipper.run_to_abs_pos(position_sp=FLIPPER_DEGREES/2,
                                    speed_sp=int(FLIPPER_SPEED/3),
                                    ramp_up_sp=100,
                                    ramp_down_sp=100,
                                    stop_action='hold')
        self.flipper.wait_while('running', timeout=1000)

        self.flipper.run_to_abs_pos(position_sp=0,
                                    speed_sp=int(FLIPPER_SPEED/3),
                                    ramp_up_sp=100,
                                    ramp_down_sp=100,
                                    stop_action='hold')
        self.flipper.wait_while('running', timeout=1000)

    def flip(self):

        if self.shutdown:
            return

        current_pos = self.flipper.position

        if current_pos >= -10 and current_pos <= 10:
            final_pos = FLIPPER_DEGREES
        else:
            final_pos = 0

        start = datetime.datetime.now()
        self.flipper.run_to_abs_pos(position_sp=final_pos,
                                    speed_sp=FLIPPER_SPEED,
                                    ramp_up_sp=100,
                                    ramp_down_sp=100,
                                    stop_action='hold')
        self.flipper.wait_while('running', timeout=1000)
        self.flipper_at_init = not self.flipper_at_init
        finish = datetime.datetime.now()
        delta_ms = (finish - start).microseconds / 1000
        log.info("flip() to final_pos %s took %dms" % (final_pos, delta_ms))
        prev_pos = None

        # Make sure we stopped where we should have
        while self.flipper.position != final_pos:
            log.info("flip() position is %d, it should be %d" % (self.flipper.position, final_pos))
            self.flipper.run_to_abs_pos(position_sp=final_pos,
                                        speed_sp=30,
                                        stop_action='hold')
            self.flipper.wait_while('running', timeout=100)

            if prev_pos is not None and self.flipper.position == prev_pos:
                break
            prev_pos = self.flipper.position

        # facing_west and facing_east won't change
        orig_north = self.facing_north
        orig_south = self.facing_south
        orig_up = self.facing_up
        orig_down = self.facing_down

        # Sometimes we flip when the elevator is raised all the way up, we do
        # this to get the flipper out of the way so we can take a pic of the
        # cube. If that is the case then then do not alter self.facing_xyz.
        if not self.rows_in_turntable:

            # We flipped from the init position to where the flipper is blocking the view of the camera
            if final_pos == FLIPPER_DEGREES:
                self.facing_north = orig_up
                self.facing_south = orig_down
                self.facing_up = orig_south
                self.facing_down = orig_north
                # log.info("flipper1 north %s, south %s, up %s, down %s" %
                #          (self.facing_north, self.facing_south, self.facing_up, self.facing_down))

            # We flipped from where the flipper is blocking the view of the camera to the init position
            else:
                self.facing_north = orig_down
                self.facing_south = orig_up
                self.facing_up = orig_north
                self.facing_down = orig_south
                # log.info("flipper2 north %s, south %s, up %s, down %s" %
                #          (self.facing_north, self.facing_south, self.facing_up, self.facing_down))

    def elevate(self, rows):
        """
        'rows' is the number of rows of the cube that should be up in the turntable

        http://studs.sariel.pl/
        - a gear rack 4 studs (32 mm) long has 9 grooves, 1 groove is 3.55555556mm
        - the gear for our elevator has 24 teeth so 360 degrees will raise the elevator by 24 grooves
        - so a 360 degree turn raises the elevator 85.333333333mm

        holder top     ----
        flipper top    ----
                          |
                          |
                          |
                          |
                          |
                          |
                          |
        flipper bottom ----
                           ^^^^
                            ||
                            ||
                            ||
                            ||
        """
        assert rows >= 0 and rows <= self.rows_and_cols, "rows was %d, rows must be between 0 and %d" % (rows, self.rows_and_cols)

        if self.shutdown:
            return

        # nothing to do
        if rows == self.rows_in_turntable:
            return

        if rows:
            # 16 studs at 8mm per stud = 128mm
            flipper_plus_holder_height_studs_mm = 122
            cube_rows_height = int((self.rows_and_cols - rows) * self.square_size_mm)
            final_pos_mm = flipper_plus_holder_height_studs_mm - cube_rows_height

            # The table in section 5 shows says that our 16 tooth gear has an outside diameter of 17.4
            # http://www.robertcailliau.eu/Alphabetical/L/Lego/Gears/Dimensions/
            diameter = 17.4
            circ = diameter * pi
            final_pos = int((final_pos_mm / circ) * 360)
        else:
            final_pos = 0

        log.info("elevate() from %d to %d, final_pos %d" % (self.rows_in_turntable, rows, final_pos))
        start = datetime.datetime.now()

        # going down
        if rows < self.rows_in_turntable:
            if final_pos:
                self.elevator.run_to_abs_pos(position_sp=final_pos,
                                             speed_sp=ELEVATOR_SPEED_DOWN_SLOW)
            else:
                # If we are dropping the elevator all the way to the bottom let
                # it drop very quickly down to 50 degrees and then lower it the
                # last 50 at a much lower speed ramp it down some so it has time
                # to stop
                self.elevator.run_to_abs_pos(position_sp=100,
                                             speed_sp=ELEVATOR_SPEED_DOWN_FAST,
                                             ramp_down_sp=100,
                                             stop_action='hold')
                self.elevator.wait_while('running')

                self.elevator.run_to_abs_pos(position_sp=0,
                                             speed_sp=ELEVATOR_SPEED_DOWN_SLOW,
                                             stop_action='hold')

        # going up
        else:
            if self.rows_in_turntable:
                self.elevator.run_to_abs_pos(position_sp=final_pos,
                                             speed_sp=ELEVATOR_SPEED_UP_SLOW,
                                             stop_action='hold')
            else:
                self.elevator.run_to_abs_pos(position_sp=final_pos,
                                             speed_sp=ELEVATOR_SPEED_UP_FAST,
                                             ramp_down_sp=100,
                                             stop_action='hold')

        self.elevator.wait_while('running')
        self.rows_in_turntable = rows
        finish = datetime.datetime.now()
        delta_ms = (finish - start).microseconds / 1000
        log.info("elevate() took %dms" % delta_ms)
        prev_pos = None

        # The elevator position has to be exact so if we went to far (this can
        # happen due to the high speeds that we use) adjust slowly so we end up
        # exactly where we need to be
        while self.elevator.position != final_pos:
            log.info("elevate() position is %d, it should be %d" % (self.elevator.position, final_pos))
            self.elevator.run_to_abs_pos(position_sp=final_pos,
                                         speed_sp=ELEVATOR_SPEED_DOWN_SLOW,
                                         stop_action='hold')
            self.elevator.wait_while('running', timeout=100)

            if prev_pos is not None and self.elevator.position == prev_pos:
                break
            prev_pos = self.elevator.position

    def elevate_max(self):
        self.elevate(self.rows_and_cols)

    def scan_face(self, name):
        log.info("scan_face() %s" % name)
        png_filename = '/tmp/rubiks-side-%s.png' % name

        if os.path.exists(png_filename):
            os.unlink(png_filename)

        # capture a single png from the webcam
        subprocess.call(['fswebcam',
                         '--device', '/dev/video0',
                         '--no-timestamp',
                         '--no-title',
                         '--no-subtitle',
                         '--no-banner',
                         '--no-info',
                         '-s', 'brightness=120%',
                         '-r', '352x240',
                         '--png', '1',
                         png_filename])

        if not os.path.exists(png_filename):
            self.shutdown = True
            return

    def scan(self):
        log.info("scan()")
        self.colors = {}
        self.scan_face('F')

        self.elevate_max()
        self.rotate(clockwise=True, quarter_turns=1)
        self.elevate(0)
        self.flip_settle_cube()
        self.scan_face('R')

        self.elevate_max()
        self.rotate(clockwise=True, quarter_turns=1)
        self.elevate(0)
        self.flip_settle_cube()
        self.scan_face('B')

        self.elevate_max()
        self.rotate(clockwise=True, quarter_turns=1)
        self.elevate(0)
        self.flip_settle_cube()
        self.scan_face('L')

        # expose the 'D' side, then raise the cube so we can get the flipper out
        # of the way, get the flipper out of the way, then lower the cube
        self.flip()
        self.elevate_max()
        self.flip()
        self.elevate(0)
        self.flip_settle_cube()
        self.scan_face('D')

        # rotate to scan the 'U' side
        self.elevate_max()
        self.rotate(clockwise=True, quarter_turns=2)
        self.elevate(0)
        self.flip_settle_cube()
        self.scan_face('U')

        # To make troubleshooting easier, move the F of the cube so that it
        # is facing the camera like it was when we started the scan
        self.flip()
        self.elevate_max()
        self.rotate(clockwise=False, quarter_turns=1)
        self.flip()
        self.elevate(0)
        self.flip_settle_cube()

        if self.shutdown:
            return

    def get_colors(self):

        if self.shutdown:
            return

        cmd = 'scp /tmp/rubiks-side-*.png robot@%s:/tmp/' % SERVER
        log.info(cmd)
        subprocess.call(cmd, shell=True)

        cube_dimensions = '%dx%dx%d' % (self.rows_and_cols, self.rows_and_cols, self.rows_and_cols)
        cmd = ['ssh',
               'robot@%s' % SERVER,
               '/home/robot/lego-crane-cuber/extract_rgb_pixels.py',
               '--size',
               str(self.rows_and_cols)]
        log.info(' '.join(cmd))
        output = subprocess.check_output(cmd).decode('ascii')
        self.colors = json.loads(output)

    def resolve_colors(self):

        if self.shutdown:
            return

        log.info("RGB json:\n%s\n" % json.dumps(self.colors))
        log.info("RGB pformat:\n%s\n" % pformat(self.colors))
        self.cube_for_resolver = subprocess.check_output(['ssh',
                                                          'robot@%s' % SERVER,
                                                          '/home/robot/rubiks-color-resolver/resolver.py',
                                                          '--rgb',
                                                          "'%s'" % json.dumps(self.colors)]).decode('ascii')
        log.info("Final Colors: %s" % self.cube_for_resolver)
        log.info("north %s, west %s, south %s, east %s, up %s, down %s" %
                    (self.facing_north, self.facing_west, self.facing_south, self.facing_east, self.facing_up, self.facing_down))

    def move_north_to_top(self):
        log.info("move_north_to_top() - flipper_at_init %s" % self.flipper_at_init)
        if self.flipper_at_init:
            self.elevate_max()
            self.rotate(clockwise=True, quarter_turns=2)

        self.elevate(0)
        self.flip()
        self.elevate(1)

    def move_west_to_top(self):
        log.info("move_west_to_top() - flipper_at_init %s" % self.flipper_at_init)
        self.elevate_max()

        if self.flipper_at_init:
            self.rotate(clockwise=False, quarter_turns=1)
        else:
            self.rotate(clockwise=True, quarter_turns=1)

        self.elevate(0)
        self.flip()
        self.elevate(1)

    def move_south_to_top(self):
        log.info("move_south_to_top() - flipper_at_init %s" % self.flipper_at_init)

        if not self.flipper_at_init:
            self.elevate_max()
            self.rotate(clockwise=True, quarter_turns=2)

        self.elevate(0)
        self.flip()
        self.elevate(1)

    def move_east_to_top(self):
        log.info("move_east_to_top() - flipper_at_init %s" % self.flipper_at_init)
        self.elevate_max()

        if self.flipper_at_init:
            self.rotate(clockwise=True, quarter_turns=1)
        else:
            self.rotate(clockwise=False, quarter_turns=1)

        self.elevate(0)
        self.flip()
        self.elevate(1)

    def get_direction(self, target_face):
        """
        target_face is in one of four locations, call them north, south, east
        and west (as viewed by looking down on the cube from the top with the camera to
        the south)

        Return the direction of target_face
        """

        if self.facing_north == target_face:
            return 'north'

        if self.facing_west == target_face:
            return 'west'

        if self.facing_south == target_face:
            return 'south'

        if self.facing_east == target_face:
            return 'east'

        raise Exception("Could not find target_face %s, north %s, west %s, south %s, east %" %
                        (target_face, self.facing_north, self.facing_west, self.facing_south, self.facing_east))

    def run_actions(self, actions):
        """
        action will be a series of moves such as
        D2 R' D' F2 B D R2 D2 R' F2 D' F2 U' B2 L2 U2 D R2 U

        The first letter is the face name
        2 means two quarter turns (rotate 180)
        ' means rotate the face counter clockwise
        """

        log.info('Moves: %s' % ' '.join(actions))
        total_actions = len(actions)

        for (index, action) in enumerate(actions):
            log.info("Move %d/%d: %s" % (index, total_actions, action))

            if self.shutdown:
                break

            if action.endswith("'") or action.endswith("’"):
                clockwise = False
            else:
                clockwise = True

            if '2' in action:
                quarter_turns = 2
            else:
                quarter_turns = 1

            target_face = action[0]
            direction = None

            if self.facing_up == 'U':
                if target_face == 'U':
                    self.elevate(1)
                elif target_face == 'D':
                    self.elevate(2)
                else:
                    direction = self.get_direction(target_face)

            elif self.facing_up == 'L':
                if target_face == 'L':
                    self.elevate(1)
                elif target_face == 'R':
                    self.elevate(2)
                else:
                    direction = self.get_direction(target_face)

            elif self.facing_up == 'F':
                if target_face == 'F':
                    self.elevate(1)
                elif target_face == 'B':
                    self.elevate(2)
                else:
                    direction = self.get_direction(target_face)

            elif self.facing_up == 'R':
                if target_face == 'R':
                    self.elevate(1)
                elif target_face == 'L':
                    self.elevate(2)
                else:
                    direction = self.get_direction(target_face)

            elif self.facing_up == 'B':
                if target_face == 'B':
                    self.elevate(1)
                elif target_face == 'F':
                    self.elevate(2)
                else:
                    direction = self.get_direction(target_face)

            elif self.facing_up == 'D':
                if target_face == 'D':
                    self.elevate(1)
                elif target_face == 'U':
                    self.elevate(2)
                else:
                    direction = self.get_direction(target_face)

            else:
                raise Exception("Invalid face %s" % self.facing_up)

            if direction:
                if direction == 'north':
                    self.move_north_to_top()
                elif direction == 'west':
                    self.move_west_to_top()
                elif direction == 'south':
                    self.move_south_to_top()
                elif direction == 'east':
                    self.move_east_to_top()

            self.rotate(clockwise, quarter_turns)
            log.info("\n\n\n\n")

    def resolve_moves(self):

        if self.shutdown:
            return

        output = subprocess.check_output(['ssh',
                                          'robot@%s' % SERVER,
                                          '/home/robot/lego-crane-cuber/kociemba_x86',
                                          self.cube_for_resolver]).decode('ascii')
        actions = output.strip().split()
        self.run_actions(actions)

        self.elevate(0)

        if not self.flipper_at_init:
            self.flip()

    def test_basics(self):
        """
        Test the three motors
        """

        input('Press ENTER to flip (to forward)')
        self.flip()

        if self.shutdown:
            return

        input('Press ENTER to flip (to init)')
        self.flip()

        if self.shutdown:
            return

        input('Press ENTER rotate 90 degrees clockwise')
        self.rotate(clockwise=True, quarter_turns=1)

        if self.shutdown:
            return

        input('Press ENTER rotate 90 degrees counter clockwise')
        self.rotate(clockwise=False, quarter_turns=1)

        if self.shutdown:
            return

        input('Press ENTER rotate 180 degrees clockwise')
        self.rotate(clockwise=True, quarter_turns=2)

        if self.shutdown:
            return

        input('Press ENTER rotate 180 degrees counter clockwise')
        self.rotate(clockwise=False, quarter_turns=2)

        if self.shutdown:
            return

        input('Press ENTER elevate to 1 rows')
        self.elevate(1)

        if self.shutdown:
            return

        input('Press ENTER elevate to lower')
        self.elevate(0)

        if self.shutdown:
            return

        input('Press ENTER elevate to 2 rows')
        self.elevate(2)

        if self.shutdown:
            return

        input('Press ENTER elevate to max rows')
        self.elevate_max()

        if self.shutdown:
            return

        input('Press ENTER elevate to 2 rows')
        self.elevate(2)

        if self.shutdown:
            return

        input('Press ENTER elevate to lower')
        self.elevate(0)

        if self.shutdown:
            return

        input('Press ENTER to rotate 1 row clockwise')
        self.elevate(1)
        self.rotate(clockwise=True, quarter_turns=1)

        if self.shutdown:
            return

        input('Press ENTER to rotate 1 row counter clockwise')
        self.elevate(1)
        self.rotate(clockwise=False, quarter_turns=1)

        if self.shutdown:
            return

        input('Press ENTER to rotate 2 row clockwise')
        self.elevate(2)
        self.rotate(clockwise=True, quarter_turns=1)

        if self.shutdown:
            return

        input('Press ENTER to rotate 2 row counter clockwise')
        self.elevate(2)
        self.rotate(clockwise=False, quarter_turns=1)

        if self.shutdown:
            return

        self.elevate(0)

    def test_patterns(self):
        """
        https://ruwix.com/the-rubiks-cube/rubiks-cube-patterns-algorithms/
        """
        tetris = ("L", "R", "F", "B", "U’", "D’", "L’", "R’")
        checkerboard = ("F", "B2", "R’", "D2", "B", "R", "U", "D’", "R", "L’", "D’", "F’", "R2", "D", "F2", "B’")

        self.run_actions(checkerboard)

        '''
        checkboard
        - 2min 38s with all speeds at 200
        - 1min 45s with 300 turn, 300 flip, 400 fast up, 800 fast down
        - 1min 29s with 400 turn, 400 flip, 800 fast up, 800 fast down
        - 1min 24s with 400 turn, 600 flip, 1050 fast up, 1050 fast down
        '''


class CraneCuber2x2x2(CraneCuber3x3x3):

    def __init__(self, rows_and_cols=2, size_mm=40):
        CraneCuber3x3x3.__init__(self, rows_and_cols, size_mm)

        # These are for a 40mm 2x2x2 cube
        self.TURN_BLOCKED_TOUCH_DEGREES = 77
        self.TURN_BLOCKED_SQUARE_CUBE_DEGREES = -117
        self.TURN_BLOCKED_SQUARE_TT_DEGREES = 40
        self.rows_in_turntable_to_count_as_face_turn = 2

    def run_actions(self, actions):
        """
        action will be a series of moves such as
        D2 R' D' F2 B D R2 D2 R' F2 D' F2 U' B2 L2 U2 D R2 U

        The first letter is the face name
        2 means two quarter turns (rotate 180)
        ' means rotate the face counter clockwise
        """

        log.info('Moves: %s' % ' '.join(actions))
        total_actions = len(actions)

        for (index, action) in enumerate(actions):
            log.info("Move %d/%d: %s" % (index, total_actions, action))
            # log.info("north %s, west %s, south %s, east %s, up %s, down %s" %
            #         (self.facing_north, self.facing_west,
            #          self.facing_south, self.facing_east,
            #          self.facing_up, self.facing_down))
            # self.wait_for_touch_sensor()

            if self.shutdown:
                break

            if action.endswith("'") or action.endswith("’"):
                clockwise = False
            else:
                clockwise = True

            if '2' in action:
                quarter_turns = 2
            else:
                quarter_turns = 1

            target_face = action[0]
            direction = None

            if self.facing_up == 'U':
                if target_face == 'U':
                    self.elevate(1)
                else:
                    direction = self.get_direction(target_face)

            elif self.facing_up == 'L':
                if target_face == 'L':
                    self.elevate(1)
                else:
                    direction = self.get_direction(target_face)

            elif self.facing_up == 'F':
                if target_face == 'F':
                    self.elevate(1)
                else:
                    direction = self.get_direction(target_face)

            elif self.facing_up == 'R':
                if target_face == 'R':
                    self.elevate(1)
                else:
                    direction = self.get_direction(target_face)

            elif self.facing_up == 'B':
                if target_face == 'B':
                    self.elevate(1)
                else:
                    direction = self.get_direction(target_face)

            elif self.facing_up == 'D':
                if target_face == 'D':
                    self.elevate(1)
                else:
                    direction = self.get_direction(target_face)

            else:
                raise Exception("Invalid face %s" % self.facing_up)

            if direction:
                if direction == 'north':
                    self.move_north_to_top()
                elif direction == 'west':
                    self.move_west_to_top()
                elif direction == 'south':
                    self.move_south_to_top()
                elif direction == 'east':
                    self.move_east_to_top()

            self.rotate(clockwise, quarter_turns)
            # log.info("north %s, west %s, south %s, east %s, up %s, down %s" %
            #         (self.facing_north, self.facing_west,
            #          self.facing_south, self.facing_east,
            #          self.facing_up, self.facing_down))
            log.info("\n\n\n\n")
            # self.wait_for_touch_sensor()

    def resolve_moves(self):

        if self.shutdown:
            return

        output = subprocess.check_output(['ssh',
                                          'robot@%s' % SERVER,
                                          '/home/robot/lego-crane-cuber/rubiks_2x2x2_solver.py',
                                          ''.join(self.cube_for_resolver)]).decode('ascii')
        if output != 'Cube is already solved':
            actions = output.strip().split()
            self.run_actions(actions)

        self.elevate(0)

        if not self.flipper_at_init:
            self.flip()


class CraneCuber4x4x4(CraneCuber3x3x3):

    def __init__(self, rows_and_cols=4, size_mm=62):
        CraneCuber3x3x3.__init__(self, rows_and_cols, size_mm)

        # These are for a 62mm 4x4x4 cube
        self.TURN_BLOCKED_TOUCH_DEGREES = 53
        self.TURN_BLOCKED_SQUARE_CUBE_DEGREES = -85
        self.TURN_BLOCKED_SQUARE_TT_DEGREES = 32
        self.rows_in_turntable_to_count_as_face_turn = 4


class CraneCuber6x6x6x(CraneCuber3x3x3):

    def __init__(self, rows_and_cols=6, size_mm=67):
        CraneCuber3x3x3.__init__(self, rows_and_cols, size_mm)

        # These are for a 67mm 6x6x6 cube
        self.TURN_BLOCKED_TOUCH_DEGREES = 29
        self.TURN_BLOCKED_SQUARE_CUBE_DEGREES = -42
        self.TURN_BLOCKED_SQUARE_TT_DEGREES = 13
        self.rows_in_turntable_to_count_as_face_turn = 6


