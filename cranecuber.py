#!/usr/bin/env python3

"""
CraneCuber
A Rubiks cube solving robot made from EV3 + 42009
"""

from copy import deepcopy
from ev3dev2.sensor.lego import TouchSensor
from ev3dev2.motor import OUTPUT_A, OUTPUT_B, OUTPUT_C, OUTPUT_D, LargeMotor, MediumMotor
from math import pi, sqrt
from pprint import pformat
from select import select
from time import sleep
from threading import Thread, Event
from time import sleep
import argparse
import datetime
import json
import logging
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys

log = logging.getLogger(__name__)

FLIPPER_DEGREES = -140

# The gear ratio is 1:2.333
# The follower gear rotates 0.428633 time per each revolution of the driver gear
# We need the follower gear to rotate 90 degrees so 90/0.428633 = 209.96
# Later on I changed gears to 1:4.666 so 420 degrees is the target now
#
# negative moves counter clockwise (viewed from above)
# positive moves clockwise (viewed from above)
TURNTABLE_TURN_DEGREES = 420
TURN_FREE_TOUCH_DEGREES = 80
TURN_FREE_SQUARE_TT_DEGREES = -80

# References
# ==========
# cube sizes
# http://cubeman.org/measure.txt
#
# README editing
# https://jbt.github.io/markdown-editor/

def round_to_quarter_turn(target_degrees):
    """
    round target_degrees up/down so that it is a multiple of TURNTABLE_TURN_DEGREES
    """
    a = int(target_degrees/TURNTABLE_TURN_DEGREES)

    if target_degrees % TURNTABLE_TURN_DEGREES == 0:
        log.info("round_to_quarter_turn %d is already a multiple of %d" % (target_degrees, TURNTABLE_TURN_DEGREES))
        return target_degrees

    log.info("round_to_quarter_turn %d/%d is %s" % (target_degrees, TURNTABLE_TURN_DEGREES, float(target_degrees/TURNTABLE_TURN_DEGREES)))
    result = int(round(float(target_degrees/TURNTABLE_TURN_DEGREES)) * TURNTABLE_TURN_DEGREES)
    log.info("round_to_quarter_turn result is %s" % result)
    return result


def convert_key_strings_to_int(data):
    result = {}
    for (key, value) in data.items():
        if isinstance(key, str) and key.isdigit():
            result[int(key)] = value
        else:
            result[key] = value
    return result


class BrokenSocket(Exception):
    pass


def send_command(ip, port, cmd):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_address = (ip, port)

    try:
        sock.connect(server_address)
    except socket.error:
        raise Exception("Could not connect to %s" % str(server_address))

    cmd = '<START>' + cmd + '<END>'
    sock.sendall(cmd.encode()) # python3
    log.info("TXed %s to cranecuberd" % cmd)

    sock.setblocking(0)
    timeout = 30
    total_data = []

    while True:
        ready = select([sock], [], [], timeout)

        if ready[0]:
            data = sock.recv(4096)

            if data:
                data = data.decode()

                if data.startswith('ERROR'):
                    raise Exception("cranecuberd %s" % data)

                total_data.append(data)
            else:
                break
        else:
            raise BrokenSocket("did not receive a response within %s seconds" % timeout)

    total_data = ''.join(total_data)
    log.info("RXed %s response" % total_data)
    sock.close()
    del(sock)

    return total_data


class CubeJammed(Exception):
    pass


class DummyMotor(object):

    def __init__(self, address):
        self.address = address
        self.position = 0
        self.state = None

    def __str__(self):
        return "DummyMotor(%s)" % self.address

    def reset(self):
        pass

    def stop(self, stop_action=None):
        pass

    def run_forever(self, speed_sp, stop_action):
        pass

    def run_to_abs_pos(self, speed_sp=None, stop_action=None, position_sp=None, ramp_up_sp=None, ramp_down_sp=None):
        pass

    def run_to_rel_pos(self, speed_sp=None, stop_action=None, position_sp=None, ramp_up_sp=None, ramp_down_sp=None):
        pass

    def wait_until(self, state, timeout=None):
        pass

    def wait_while(self, state, timeout=None):
        pass

    def wait_until_not_moving(self, timeout=None):
        pass


class DummySensor(object):

    def is_pressed(self):
        return True


class CraneCuber3x3x3(object):

    def __init__(self, SERVER, emulate, rows_and_cols=3, size_mm=57):
        self.SERVER = SERVER
        self.shutdown_event = Event()
        self.rows_and_cols = rows_and_cols
        self.size_mm = size_mm
        self.square_size_mm = float(self.size_mm / self.rows_and_cols)
        self.emulate = emulate
        self.cube_for_resolver = None
        self.mts = None
        self.waiting_for_touch_sensor = Event()
        self.move_north_to_top_calls = 0
        self.move_south_to_top_calls = 0
        self.move_east_to_top_calls = 0
        self.move_west_to_top_calls = 0
        self.move_down_to_top_calls = 0

        if self.emulate:
            self.elevator = DummyMotor(OUTPUT_A)
            self.flipper = DummyMotor(OUTPUT_B)
            self.turntable = DummyMotor(OUTPUT_C)
            self.squisher = DummyMotor(OUTPUT_D)
        else:
            self.elevator = LargeMotor(OUTPUT_A)
            self.flipper = MediumMotor(OUTPUT_B)
            self.turntable = LargeMotor(OUTPUT_C)
            self.squisher = LargeMotor(OUTPUT_D)

        #self.elevator.total_distance = 0
        #self.flipper.total_distance = 0
        #self.turntable.total_distance = 0
        #self.squisher.total_distance = 0

        self.motors = [self.elevator, self.flipper, self.turntable, self.squisher]
        self.rows_in_turntable = 0
        self.facing_north = 'B'
        self.facing_west = 'L'
        self.facing_south = 'F'
        self.facing_east = 'R'
        self.facing_up = 'U'
        self.facing_down = 'D'
        signal.signal(signal.SIGTERM, self.signal_term_handler)
        signal.signal(signal.SIGINT, self.signal_int_handler)
        self.time_elevate = 0
        self.time_flip = 0
        self.time_rotate = 0
        self.flipper_at_init = True
        self.colors = {}

        # positive moves to init position
        # negative moves towards camera
        self.FLIPPER_SPEED = 300

        # Slow down for more accuracy
        # positive is clockwise
        # negative is counter clockwise
        self.TURNTABLE_SPEED_NORMAL = 1050
        self.TURNTABLE_SPEED_FREE = 1050

        # positive moves down
        # negative moves up
        self.ELEVATOR_SPEED_UP_FAST = 1050
        self.ELEVATOR_SPEED_UP_SLOW = 1050
        self.ELEVATOR_SPEED_DOWN_FAST = 1050
        self.ELEVATOR_SPEED_DOWN_SLOW = 1050

        # These numbers are for a 57mm 3x3x3 cube
        self.TURN_BLOCKED_TOUCH_DEGREES = 214
        self.TURN_BLOCKED_SQUARE_TT_DEGREES = 80
        self.TURN_BLOCKED_SQUARE_CUBE_DEGREES = -300
        self.SQUISH_DEGREES = 120
        self.rows_in_turntable_to_count_as_face_turn = 2

    def init_motors(self):

        # 'brake' stops but doesn't hold the motor in place
        # 'hold' stops and holds the motor in place

        # Lower all the way down, then raise a bit, then lower back down.
        # We do this to make sure it is in the same starting spot each time.
        log.info("Initialize elevator %s - lower all the way down" % self.elevator)
        self.elevator.run_forever(speed_sp=30, stop_action='brake')
        self.elevator.wait_until('running')
        self.elevator.wait_until_not_moving(timeout=10000)
        self.elevator.stop()
        self.elevator.reset()

        log.info("Initialize elevator %s - raise a bit" % self.elevator)
        self.elevator.run_to_rel_pos(speed_sp=200, position_sp=-50)
        self.elevator.wait_until('running')
        self.elevator.wait_until_not_moving()

        log.info("Initialize elevator %s - lower back down" % self.elevator)
        self.elevator.run_forever(speed_sp=20, stop_action='hold')
        self.elevator.wait_until('running')
        self.elevator.wait_until_not_moving(timeout=4000)
        self.elevator.stop()
        self.elevator.reset()
        self.elevator.stop(stop_action='brake')

        log.info("Initialize flipper %s" % self.flipper)
        self.flipper.run_forever(speed_sp=150, stop_action='hold')
        self.flipper.wait_until('running')
        self.flipper.wait_until('stalled')
        self.flipper.stop()
        self.flipper.reset()
        self.flipper.stop(stop_action='hold')
        self.flipper_at_init = True

        log.info("Initialize turntable %s" % self.turntable)
        self.turntable.reset()
        self.turntable.stop(stop_action='hold')

        log.info("Initialize squisher %s" % self.squisher)
        self.squisher_reset()
        self.squisher.stop(stop_action='brake')

    def shutdown_robot(self):

        if self.shutdown_event.is_set():
            return

        self.shutdown_event.set()
        log.info('shutting down')

        if self.mts:
            log.info('shutting down mts')
            self.mts.shutdown_event.set()
            self.mts.join()
            self.mts = None
            log.info('shutting down mts complete')

        self.elevate(0)

        #if self.flipper.position > 10:
        #    self.flip()

        for x in self.motors:
            x.stop(stop_action='brake')

    def signal_term_handler(self, signal, frame):
        log.error('Caught SIGTERM')
        self.shutdown_robot()

    def signal_int_handler(self, signal, frame):
        log.error('Caught SIGINT')
        self.shutdown_robot()

    def _rotate(self, final_pos, must_be_accurate, count_total_distance):

        if must_be_accurate:
            speed = self.TURNTABLE_SPEED_NORMAL
            ramp_up = 200
            ramp_down = 500
        else:
            speed = self.TURNTABLE_SPEED_FREE
            ramp_up = 0
            ramp_down = 0

        start_pos = self.turntable.position
        delta = abs(final_pos - start_pos)

        #if count_total_distance:
        #    self.turntable.total_distance += delta

        if not delta:
            return

        # We must turn the squisher in the opposite direction so that we do not fight
        # it the entire time we are rotating
        #
        # The gear ratio for the squisher is 1.8:1
        # The gear ratio for the turntable is 1:4.666
        # positive closes the squisher
        # negative opens the squisher
        if final_pos > start_pos:
            # In this direction the squisher wants to open/unsquish so we must
            # close it to keep it in the same position
            squisher_position = (delta / 4.666) / 1.8
        else:
            # In this direction the squisher wants to close/squish so we must
            # open it to keep it in the same position
            squisher_position = (delta / 4.666) / -1.8

        time_to_rotate = float(delta/speed)
        squisher_speed = abs(int(squisher_position / time_to_rotate))

        # We must rotate the squisher in the opposite direction so that it
        # remains in the same place while the turntable is rotating
        self.turntable.run_to_abs_pos(position_sp=final_pos,
                                      speed_sp=speed,
                                      stop_action='hold',
                                      ramp_up_sp=ramp_up,
                                      ramp_down_sp=ramp_down)
        self.turntable.wait_until('running', timeout=2000)
        self.squisher.run_to_rel_pos(position_sp=squisher_position, speed_sp=squisher_speed)
        self.squisher.wait_until('running', timeout=2000)

        # Now wait for both to stop
        self.turntable.wait_until_not_moving(timeout=2000)
        self.squisher.wait_until_not_moving(timeout=2000)

        #log.info("delta %s, speed %s, time_to_rotate_ %s, squisher_position %s, squisher_speed %s" % (delta, speed, time_to_rotate, squisher_position, squisher_speed))
        log.info("end _rotate() to %s, speed %d, must_be_accurate %s, %s is %s went %s->%s, squisher %s" %\
            (final_pos, speed, must_be_accurate,
             self.turntable, self.turntable.state, start_pos, self.turntable.position, self.squisher.position))

    def rotate(self, clockwise, quarter_turns, count_total_distance=False):

        if self.shutdown_event.is_set():
            return

        assert quarter_turns > 0 and quarter_turns <= 2, "quarter_turns is %d, it must be between 0 and 2" % quarter_turns
        current_pos = self.turntable.position
        start = datetime.datetime.now()

        # cube will turn freely since none of the rows are being held
        if self.rows_in_turntable == self.rows_and_cols:
            turn_degrees = TURN_FREE_TOUCH_DEGREES + (TURNTABLE_TURN_DEGREES * quarter_turns)
            square_turntable_degrees = TURN_FREE_SQUARE_TT_DEGREES

            if not clockwise:
                turn_degrees *= -1
                square_turntable_degrees *= -1

            turn_pos = current_pos + turn_degrees
            square_turntable_pos = round_to_quarter_turn(turn_pos + square_turntable_degrees)

            self._rotate(turn_pos, False, count_total_distance)
            self._rotate(square_turntable_pos, False, count_total_distance)

            finish = datetime.datetime.now()
            delta_ms = ((finish - start).seconds * 1000) + ((finish - start).microseconds / 1000)
            self.time_rotate += delta_ms

            log.info("rotate_cube() FREE %d quarter turns, clockwise %s), current_pos %d, turn_pos %d, square_turntable_pos %d took %dms" %
                (quarter_turns, clockwise, current_pos, turn_pos, square_turntable_pos, delta_ms))

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
            square_turntable_pos = round_to_quarter_turn(square_cube_pos + square_turntable_degrees)
            self._rotate(turn_pos, True, count_total_distance)

            # The larger cubes are such a tight fit they do not need the wiggle move to square them up
            if self.rows_and_cols <= 5:
                self._rotate(square_cube_pos, False, count_total_distance)

            self._rotate(square_turntable_pos, False, count_total_distance)

            finish = datetime.datetime.now()
            delta_ms = ((finish - start).seconds * 1000) + ((finish - start).microseconds / 1000)
            self.time_rotate += delta_ms
            log.info("rotate_cube() BLOCKED %d quarter turns, clockwise %s, current_pos %d, turn_pos %d, square_cube_pos %d, square_turntable_pos %d took %dms" %
                (quarter_turns, clockwise, current_pos, turn_pos, square_cube_pos, square_turntable_pos, delta_ms))

        # Only update the facing_XYZ variables if the entire side is turning.  For
        # a 3x3x3 this means the middle square is being turned, this happens if at
        # least two rows are up in the turntable
        if self.rows_in_turntable >= self.rows_in_turntable_to_count_as_face_turn:
            orig_north = self.facing_north
            orig_west = self.facing_west
            orig_south = self.facing_south
            orig_east = self.facing_east
            orig_up = self.facing_up
            orig_down = self.facing_down

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

            #log.warning("north %s, west %s, south %s, east %s, up %s, down %s (original), rows_in_turntable %d, rows_in_turntable_to_count_as_face_turn %d" %
            #    (orig_north, orig_west, orig_south, orig_east, orig_up, orig_down, self.rows_in_turntable, self.rows_in_turntable_to_count_as_face_turn))

        #log.info("north %s, west %s, south %s, east %s, up %s, down %s" %
        #    (self.facing_north, self.facing_west, self.facing_south, self.facing_east, self.facing_up, self.facing_down))

    def squish(self):
        # positive closes the squisher
        self.turntable.stop(stop_action='hold')
        self.squisher.reset()
        self.squisher.run_to_rel_pos(position_sp=self.SQUISH_DEGREES, speed_sp=400, stop_action='brake')
        self.squisher.wait_until('running')
        self.squisher.wait_until_not_moving(timeout=5000)
        self.squisher.stop()

        # negative opens the squisher
        self.squisher.run_to_rel_pos(position_sp=self.SQUISH_DEGREES * -1, speed_sp=400, stop_action='coast')
        self.squisher.wait_until('running')
        self.squisher.wait_until_not_moving(timeout=2000)
        self.squisher.stop()
        self.turntable.stop(stop_action='brake')

    def squisher_reset(self):
        self.squisher.run_forever(speed_sp=-40, stop_action='coast')
        self.squisher.wait_until('running')
        self.squisher.wait_until_not_moving(timeout=4000)
        self.squisher.reset()

    def flip_settle_cube(self):
        """
        Even though this move looks like it isn't needed, it is.  The reason
        being if you don't tilt the flipper to get the cube to slide back, when
        we go to raise the cube it is too easy for it to jam up because it may
        have slid forward too far.
        """

        if self.shutdown_event.is_set():
            return

        log.info("flip_settle_cube run_to_abs_pos -60")
        self.flipper.run_to_abs_pos(position_sp=-60,
                                    speed_sp=self.FLIPPER_SPEED,
                                    ramp_up_sp=0,
                                    ramp_down_sp=0,
                                    stop_action='hold')
        self.flipper.wait_until('running')
        self.flipper.wait_until_not_moving(timeout=2000)

        if self.shutdown_event.is_set():
            return

        log.info("flip_settle_cube run_to_abs_pos 0")
        self.flipper.run_to_abs_pos(position_sp=0,
                                    speed_sp=self.FLIPPER_SPEED/2,
                                    ramp_up_sp=0,
                                    ramp_down_sp=500,
                                    stop_action='hold')
        self.flipper.wait_until('running')
        self.flipper.wait_until_not_moving(timeout=2000)

    def flip_to_init(self):

        if abs(self.flipper.position) >= abs(int(FLIPPER_DEGREES/2)):
            self.flip()

    def flip(self, slow=False):

        if self.shutdown_event.is_set():
            log.info("flip shutdown_event is set")
            return

        init_pos = self.flipper.position

        # positive moves to init position
        # negative moves towards camera
        if abs(init_pos) <= abs(int(FLIPPER_DEGREES/2)):
            final_pos = FLIPPER_DEGREES
        else:
            final_pos = 0

        # If you flip too fast the momentum can cause the cube to slide a
        # little when the flipper stops.  When the cube slides like this it is
        # no longer lined up with the turntable above so when we raise the
        # cube it jams up.
        start = datetime.datetime.now()

        # If the elevator is raised then the cube is not in the flipper so we
        # can flip it pretty quickly.  If the cube is in the flipper though we
        # have to flip more slowly, if you flip too fast the momentum can cause
        # the cube to slide a little when the flipper stops.  When the cube
        # slides like this it is no longer lined up with the turntable above so
        # when we raise the cube it jams up.
        log.info("flipper run_to_abs_pos(), rows_in_turntable %s, flipper_at_init %s, init_pos %s, final_pos %s" % (self.rows_in_turntable, self.flipper_at_init, init_pos, final_pos))

        if self.rows_in_turntable == 0:

            if slow:
                flipper_speed = int(self.FLIPPER_SPEED / 4)
            else:
                flipper_speed = self.FLIPPER_SPEED

            self.flipper.run_to_abs_pos(position_sp=final_pos,
                                        speed_sp=flipper_speed,
                                        ramp_up_sp=0,
                                        ramp_down_sp=500,
                                        stop_action='hold')

        # Cube is raised so we can go fast
        else:
            self.flipper.run_to_abs_pos(position_sp=final_pos,
                                        speed_sp=self.FLIPPER_SPEED * 2,
                                        ramp_up_sp=0,
                                        ramp_down_sp=0,
                                        stop_action='hold')

        log.info("flipper wait_until running")
        self.flipper.wait_until('running')
        log.info("flipper running wait_until_not_moving")
        self.flipper.wait_until_not_moving(timeout=4000)
        self.flipper_at_init = not self.flipper_at_init
        current_pos = self.flipper.position
        log.info("flipper not moving, at_init %s" % self.flipper_at_init)

        finish = datetime.datetime.now()
        delta_ms = ((finish - start).seconds * 1000) + ((finish - start).microseconds / 1000)
        self.time_flip += delta_ms
        degrees_moved = abs(current_pos - init_pos)
        log.info("flip() %s degrees (%s -> %s, target %s) took %dms" %
            (degrees_moved, init_pos, current_pos, final_pos, delta_ms))

        # This shouldn't happen anymore now that we tilt the flipper a few
        # degrees when we elevate() the cube up so that it is flush against
        # the flipper when it comes back down.
        if not self.emulate and abs(degrees_moved) < abs(int(FLIPPER_DEGREES/2)):
            raise CubeJammed("jammed on flip, moved %d degrees" % abs(degrees_moved))

        if final_pos == 0 and current_pos != final_pos:
            self.flipper.reset()
            self.flipper.stop(stop_action='hold')

        # facing_west and facing_east won't change
        orig_north = self.facing_north
        orig_south = self.facing_south
        orig_up = self.facing_up
        orig_down = self.facing_down

        # Sometimes we flip when the elevator is raised all the way up, we do
        # this to get the flipper out of the way so we can take a pic of the
        # cube. If that is the case then then do not alter self.facing_xyz.
        if self.rows_in_turntable == 0:

            # We flipped from the init position to where the flipper is blocking the view of the camera
            if abs(final_pos - FLIPPER_DEGREES) <= 20:
                self.facing_north = orig_up
                self.facing_south = orig_down
                self.facing_up = orig_south
                self.facing_down = orig_north
                #log.info("flipper1 north %s, west %s, south %s, east %s, up %s, down %s" %
                #    (self.facing_north, self.facing_west, self.facing_south, self.facing_east, self.facing_up, self.facing_down))

            # We flipped from where the flipper is blocking the view of the camera to the init position
            else:
                self.facing_north = orig_down
                self.facing_south = orig_up
                self.facing_up = orig_north
                self.facing_down = orig_south
                #log.info("flipper2 north %s, west %s, south %s, east %s, up %s, down %s" %
                #    (self.facing_north, self.facing_west, self.facing_south, self.facing_east, self.facing_up, self.facing_down))

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
        log.info("elevate called for rows %d, rows_in_turntable %d" % (rows, self.rows_in_turntable))
        assert 0 <= rows <= self.rows_and_cols, "rows was %d, rows must be between 0 and %d" % (rows, self.rows_and_cols)

        if self.shutdown_event.is_set():
            log.info("elevate: shutdown_event is set")
            return

        # nothing to do
        if rows == self.rows_in_turntable:
            log.info("elevate: rows == rows_in_turntable nothing to do")
            return

        # The table in section 5 shows says that our 16 tooth gear has an outside diameter of 17.4
        # http://www.robertcailliau.eu/Alphabetical/L/Lego/Gears/Dimensions/
        diameter = 17.4
        circ = diameter * pi
        final_pos_mm = 0

        # 16 studs at 8mm per stud = 128mm
        flipper_plus_holder_height_studs_mm = 134

        if rows:

            if self.rows_and_cols == 2:
                if rows == 1:
                    final_pos = -207
                elif rows == 2:
                    final_pos = -285
                else:
                    raise Exception("2x2x2 does not have %d rows" % rows)

            elif self.rows_and_cols == 3:
                if rows == 1:
                    final_pos = -182
                elif rows == 2:
                    final_pos = -224
                elif rows == 3:
                    final_pos = -281
                else:
                    raise Exception("3x3x3 does not have %d rows" % rows)

            elif self.rows_and_cols == 4:
                if rows == 1:
                    final_pos = -159
                elif rows == 2:
                    final_pos = -197
                elif rows == 3:
                    final_pos = -235
                elif rows == 4:
                    final_pos = -280
                else:
                    raise Exception("4x4x4 does not have %d rows" % rows)

            elif self.rows_and_cols == 5:
                if rows == 1:
                    final_pos = -152
                elif rows == 2:
                    final_pos = -185
                elif rows == 3:
                    final_pos = -210
                elif rows == 4:
                    final_pos = -239
                elif rows == 5:
                    final_pos = -275
                else:
                    raise Exception("5x5x5 does not have %d rows" % rows)

            elif self.rows_and_cols == 6:
                if rows == 1:
                    final_pos = -145
                elif rows == 2:
                    final_pos = -170
                elif rows == 3:
                    final_pos = -197
                elif rows == 4:
                    final_pos = -217
                elif rows == 5:
                    final_pos = -248
                elif rows == 6:
                    final_pos = -272
                else:
                    raise Exception("6x6x6 does not have %d rows" % rows)

            elif self.rows_and_cols == 7:
                if rows == 1:
                    final_pos = -132
                elif rows == 2:
                    final_pos = -154
                elif rows == 3:
                    final_pos = -177
                elif rows == 4:
                    final_pos = -193
                elif rows == 5:
                    final_pos = -220
                elif rows == 6:
                    final_pos = -240
                elif rows == 7:
                    final_pos = -263
                else:
                    raise Exception("7x7x7 does not have %d rows" % rows)

            else:
                raise Exception("%dx%dx%d cubes are not supported" % (self.rows_and_cols, self.rows_and_cols, self.rows_and_cols))

            final_pos -= 15
        else:
            final_pos = 0

        start = datetime.datetime.now()
        init_pos = self.elevator.position
        #self.elevator.total_distance += abs(final_pos - init_pos)

        # going down
        if rows < self.rows_in_turntable:
            # If we are lowering the cube we have to use a ramp_up because if we
            # drop the cube too suddenly it tends to jam up
            log.info("elevate down: final_pos %s, run_to_abs()" % final_pos)

            # drop the cube a few more rows
            if final_pos:
                self.elevator.run_to_abs_pos(position_sp=final_pos,
                                             speed_sp=self.ELEVATOR_SPEED_DOWN_SLOW,
                                             ramp_up_sp=200,
                                             ramp_down_sp=200, # ramp_down so we stop at the right spot
                                             stop_action='hold')
            # go all the way to the bottom
            else:
                self.elevator.run_to_abs_pos(position_sp=0,
                                             speed_sp=self.ELEVATOR_SPEED_DOWN_FAST,
                                             ramp_up_sp=500,
                                             ramp_down_sp=400,
                                             stop_action='hold')
            log.info("elevate down: wait_until running")
            self.elevator.wait_until('running')
            log.info("elevate down: running, wait_until_not_moving")
            self.elevator.wait_until_not_moving(timeout=3000)
            log.info("elevate down: not_moving")

        # going up
        else:
            log.info("elevate up: rows_in_turntable %s, run_to_abs()" % self.rows_in_turntable)

            # raise the cube a few more rows
            if self.rows_in_turntable:
                self.elevator.run_to_abs_pos(position_sp=final_pos,
                                             speed_sp=self.ELEVATOR_SPEED_UP_SLOW,
                                             ramp_up_sp=200,
                                             ramp_down_sp=50,
                                             stop_action='hold')
            # starting out at the bottom
            else:
                self.elevator.run_to_abs_pos(position_sp=final_pos,
                                             speed_sp=self.ELEVATOR_SPEED_UP_FAST,
                                             ramp_up_sp=200, # ramp_up here so we don't slam into the cube at full speed
                                             ramp_down_sp=50, # ramp_down so we stop at the right spot
                                             stop_action='hold')

            log.info("elevate up: wait_until running")
            self.elevator.wait_until('running')
            log.info("elevate up: running, wait_until_not_moving")
            self.elevator.wait_until_not_moving(timeout=3000)
            log.info("elevate up: not_moving")

            # Did we jam up?
            current_pos = self.elevator.position
            delta = abs(current_pos - init_pos)
            delta_target = abs(final_pos - init_pos)

            if delta < (delta_target * 0.90):
                log.warning("elevate jammed up, only moved %d, should have moved %d...attempting to clear" % (delta, delta_target))
                self.elevator.run_to_abs_pos(position_sp=0,
                                             speed_sp=self.ELEVATOR_SPEED_UP_SLOW,
                                             stop_action='hold')
                self.elevator.wait_until('running')
                self.elevator.wait_until_not_moving(timeout=3000)

                self.squisher_reset()
                self.flip(slow=True)
                self.flip(slow=True)

                self.elevator.run_to_abs_pos(position_sp=final_pos,
                                             speed_sp=self.ELEVATOR_SPEED_UP_FAST,
                                             ramp_up_sp=200, # ramp_up here so we don't slam into the cube at full speed
                                             ramp_down_sp=50, # ramp_down so we stop at the right spot
                                             stop_action='hold')
                self.elevator.wait_until('running')
                self.elevator.wait_until_not_moving(timeout=3000)

                current_pos = self.elevator.position
                delta = abs(current_pos - init_pos)
                delta_target = abs(final_pos - init_pos)

                if delta < (delta_target * 0.90):
                    raise CubeJammed("elevate jammed up, only moved %d, should have moved %d" % (delta, delta_target))

        finish = datetime.datetime.now()
        delta_ms = ((finish - start).seconds * 1000) + ((finish - start).microseconds / 1000)
        self.time_elevate += delta_ms
        log.info("elevate() from %d to %d took %dms, final_pos target %s, position %s, final_pos_mm %d" %
            (self.rows_in_turntable, rows, delta_ms, final_pos, self.elevator.position, final_pos_mm))
        self.rows_in_turntable = rows

        if final_pos == 0 and self.elevator.position != final_pos:
            self.elevator.reset()
            self.elevator.stop(stop_action='hold')

    def elevate_max(self):
        self.elevate(self.rows_and_cols)

    def scan_face(self, name):

        if self.shutdown_event.is_set():
            return

        log.info("scan_face() %s" % name)
        png_filename = '/tmp/rubiks-side-%s.png' % name

        if self.emulate:
            shutil.copy('/home/dwalton/lego/rubiks-cube-tracker/test/test-data/7x7x7-random-02/rubiks-side-%s.png' % name, png_filename)
        else:
            send_command(self.SERVER, 10000, "TAKE_PICTURE:%s" % name)

    def scan(self):

        if self.shutdown_event.is_set():
            return

        log.info("scan()")
        self.colors = {}
        self.flip_settle_cube()
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

        #log.info("Paused")
        #input("Paused")

    def get_colors(self):

        if self.shutdown_event.is_set():
            return

        # Ask cranecuberd to extract the colors from the six cube images
        output = send_command(self.SERVER, 10000, "GET_RGB_COLORS").strip()

        if not output:
            raise Exception("GET_RGB_COLORS did not return any output")

        log.info("GET_RGB_COLORS\n%s\n" % output)
        self.colors = json.loads(output)

    def resolve_colors(self):

        if self.shutdown_event.is_set():
            return

        cmd = "GET_CUBE_STATE:%s" % json.dumps(self.colors)
        output = send_command(self.SERVER, 10000, cmd)
        self.resolved_colors = json.loads(output)

        self.resolved_colors['squares'] = convert_key_strings_to_int(self.resolved_colors['squares'])
        self.cube_for_resolver = self.resolved_colors['kociemba']

        log.info("Final Colors: %s" % self.cube_for_resolver)
        log.info("north %s, west %s, south %s, east %s, up %s, down %s" %
                 (self.facing_north, self.facing_west, self.facing_south, self.facing_east, self.facing_up, self.facing_down))

    def flip_with_elevator_clear(self):

        if self.rows_in_turntable:
            raise Exception("Do not call when rows are in turntable (%d)" % self.rows_in_turntable)

        self.elevate(1)
        self.flip()
        self.elevate(0)

    def move_north_to_top(self, rows):
        log.info("move_north_to_top() - flipper_at_init %s, rows %d" % (self.flipper_at_init, rows))

        # There are four starting points
        # flipper at init, elevator rows in turntable
        # flipper at init, elevator no rows in turntable
        # flipper not at init, elevator rows in turntable
        # flipper not at init, elevator no rows in turntable

        # We need to get to the state of
        #   flipper not at init, elevator no rows in turntable

        if self.flipper_at_init and self.rows_in_turntable:
            self.flip()
            self.elevate(0)

        elif self.flipper_at_init and not self.rows_in_turntable:
            self.flip_with_elevator_clear()

        elif not self.flipper_at_init and self.rows_in_turntable:
            self.elevate(0)

        elif not self.flipper_at_init and not self.rows_in_turntable:
            pass

        else:
            raise Exception("self.flipper_at_init %s, self.rows_in_turntable %s" % (self.flipper_at_init, self.rows_in_turntable))

        self.flip()
        self.elevate(rows)
        self.move_north_to_top_calls += 1

    def move_west_to_top(self, rows):
        log.info("move_west_to_top() - flipper_at_init %s, rows %d" % (self.flipper_at_init, rows))
        self.elevate_max()

        # Since we have the cube raised up as far as it can go, go
        # ahead and squish it to re-align everything
        if self.rows_and_cols < 6:
            self.squish()

        if self.flipper_at_init:
            self.rotate(clockwise=False, quarter_turns=1, count_total_distance=True)
        else:
            self.rotate(clockwise=True, quarter_turns=1, count_total_distance=True)

        self.elevate(0)
        self.flip()
        self.elevate(rows)
        self.move_west_to_top_calls += 1

    def move_south_to_top(self, rows):
        log.info("move_south_to_top() - flipper_at_init %s, rows %d" % (self.flipper_at_init, rows))

        # There are four starting points
        # flipper at init, elevator rows in turntable
        # flipper at init, elevator no rows in turntable
        # flipper not at init, elevator rows in turntable
        # flipper not at init, elevator no rows in turntable

        # We need to get to the state of
        #   flipper at init, elevator no rows in turntable

        if self.flipper_at_init and self.rows_in_turntable:
            self.elevate(0)

        elif self.flipper_at_init and not self.rows_in_turntable:
            pass

        elif not self.flipper_at_init and self.rows_in_turntable:
            self.flip()
            self.elevate(0)

        elif not self.flipper_at_init and not self.rows_in_turntable:
            self.flip_with_elevator_clear()

        else:
            raise Exception("self.flipper_at_init %s, self.rows_in_turntable %s" % (self.flipper_at_init, self.rows_in_turntable))

        self.flip()
        self.elevate(rows)
        self.move_south_to_top_calls += 1

    def move_east_to_top(self, rows):
        """
        Each move east/west _to_top does 2.18 quarter turns due to having to over-rotate
        and then rotate the turntable back a bit. What if we used the squisher to
        hold the cube so that we didn't have to over-rotate at all? The challenge
        there is the cube would be pressed up all the way against the side and would
        be more likely to jam up on the elevate(0). I'll stick with what I have :)
        """
        log.info("move_east_to_top() - flipper_at_init %s, rows %d" % (self.flipper_at_init, rows))
        self.elevate_max()

        # Since we have the cube raised up as far as it can go, go
        # ahead and squish it to re-align everything
        if self.rows_and_cols < 6:
            self.squish()

        if self.flipper_at_init:
            self.rotate(clockwise=True, quarter_turns=1, count_total_distance=True)
        else:
            self.rotate(clockwise=False, quarter_turns=1, count_total_distance=True)

        self.elevate(0)
        self.flip()
        self.elevate(rows)
        self.move_east_to_top_calls += 1

    def move_down_to_top(self, rows):
        log.info("move_down_to_top() - flipper_at_init %s, rows %d" % (self.flipper_at_init, rows))
        self.elevate(0)
        self.flip()
        self.flip_with_elevator_clear()
        self.flip()
        self.elevate(rows)
        self.move_down_to_top_calls += 1

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

        if self.facing_down == target_face:
            return 'down'

        raise Exception("Could not find target_face %s, north %s, west %s, south %s, east %s, up %s, down %s" %
                        (target_face,
                         self.facing_north, self.facing_west, self.facing_south, self.facing_east,
                         self.facing_up, self.facing_down))

    def run_solution(self, actions):
        """
        action will be a series of moves such as
        D'  B2  Rw' Uw  R2  Fw  D   Rw2 B   R2  Uw  D2  Rw2 U2  Fw2 U2  L   F
        R   Uw2 B'  R   Uw2 L'  D   B   L2  U   B2  D   B2  F'  U'  R   B2  R2
        F2  R'  B2  F2  D2  L'  U2  z'

        https://www.randelshofer.ch/cubetwister/doc/notations/wca_4x4.html
        - the first letter is the face name
        - the w means turn both layers of that face
        - 2 means two quarter turns (rotate 180)
        - ' means rotate counter clockwise
        - ignore the x, y, z at the end, this is just rotating the entire cube to get the F side back to the front
        """

        log.info('Moves: %s' % ' '.join(actions))
        total_actions = len(actions)
        start = datetime.datetime.now()
        moves = 0
        self.time_elevate = 0
        self.time_flip = 0
        self.time_rotate = 0
        debug = False

        # If use_shortcut is True and we do back-to-back set of moves on opposite
        # faces (like "F B") do not bother flipping the cube around to make B face
        # up, just rotate with F facing up.
        #
        # 2x2x2 - does not apply since solver only uses U F and R
        # 3x3x3 - works just fine
        # 4x4x4 - does not work...cube doesn't solve...need to investigate
        # 5x5x5 - works just fine
        if self.rows_and_cols in (3, 5, 7):
            use_shortcut = True
        else:
            use_shortcut = False

        '''
        For our 7x7x7 cube in --emulate

        without use_shortcut
2018-02-02 12:51:33,120 crane_cuber.py     INFO: DummyMotor(outA): elevator moved 75740 degrees total (210 rotations)
2018-02-02 12:51:33,120 crane_cuber.py     INFO: DummyMotor(outC): turntable moved 89240 degrees total (212 quarter turns)
2018-02-02 12:51:33,120 crane_cuber.py     INFO: 53 move_north_to_top_calls
2018-02-02 12:51:33,120 crane_cuber.py     INFO: 58 move_south_to_top_calls
2018-02-02 12:51:33,120 crane_cuber.py     INFO: 54 move_east_to_top_calls
2018-02-02 12:51:33,120 crane_cuber.py     INFO: 43 move_west_to_top_calls
2018-02-02 12:51:33,120 crane_cuber.py     INFO: 36 move_down_to_top_calls
2018-02-02 12:51:33,120 crane_cuber.py     INFO: 244 move_calls total
2018-02-02 12:51:33,120 crane_cuber.py     INFO: 97 move_calls (east/west) total

        with use_shortcut...saves 36 move_down_to_top() calls
2018-02-02 12:52:07,943 crane_cuber.py     INFO: DummyMotor(outA): elevator moved 73382 degrees total (203 rotations)
2018-02-02 12:52:07,943 crane_cuber.py     INFO: DummyMotor(outC): turntable moved 92920 degrees total (221 quarter turns)
2018-02-02 12:52:07,943 crane_cuber.py     INFO: 49 move_north_to_top_calls
2018-02-02 12:52:07,943 crane_cuber.py     INFO: 71 move_south_to_top_calls
2018-02-02 12:52:07,943 crane_cuber.py     INFO: 56 move_east_to_top_calls
2018-02-02 12:52:07,943 crane_cuber.py     INFO: 45 move_west_to_top_calls
2018-02-02 12:52:07,943 crane_cuber.py     INFO: 0 move_down_to_top_calls
2018-02-02 12:52:07,943 crane_cuber.py     INFO: 221 move_calls total
2018-02-02 12:52:07,944 crane_cuber.py     INFO: 101 move_calls (east/west) total

        Each each east/west move is 2.18 quarter turns
        '''

        for (index, action) in enumerate(actions):
            desc = "Move %d/%d : %s" % (index, total_actions, action)
            print(desc)
            log.info(desc)

            if self.shutdown_event.is_set():
                break

            if action.startswith('x') or action.startswith('y') or action.startswith('z'):
                continue

            if action.endswith("'") or action.endswith("’"):
                action = action[0:-1]
                clockwise = False
            else:
                clockwise = True

            if action.endswith('2'):
                quarter_turns = 2
                action = action[0:-1]
            elif action.endswith('1'):
                quarter_turns = 1
                action = action[0:-1]
            else:
                quarter_turns = 1

            re_number_side_w = re.search('^(\d+)(\w+)w', action)
            re_side_w = re.search('^(\w+)w', action)
            direction = None

            if re_number_side_w:
                rows = int(re_number_side_w.group(1))
                target_face = re_number_side_w.group(2)

            elif re_side_w:
                rows = 2
                target_face = re_side_w.group(1)

            else:
                target_face = action[0]
                rows = 1

            log.info("Up %s, Down %s, North %s, West %s, South %s, East %s, target_face %s, rows %d, quarter_turns %d, clockwise %s" %
                    (self.facing_up, self.facing_down, self.facing_north, self.facing_west, self.facing_south, self.facing_east,
                     target_face, rows, quarter_turns, clockwise))

            if rows == self.rows_and_cols:
                pass

            elif rows >= self.rows_in_turntable_to_count_as_face_turn:
                raise Exception("CraneCuber does not support %s for this size cube" % action)

            if self.facing_up == 'U':
                if target_face == 'U':
                    self.elevate(rows)
                elif use_shortcut and target_face == 'D':
                    rows = self.rows_and_cols - rows
                    self.elevate(rows)
                else:
                    direction = self.get_direction(target_face)

            elif self.facing_up == 'L':
                if target_face == 'L':
                    self.elevate(rows)
                elif use_shortcut and target_face == 'R':
                    rows = self.rows_and_cols - rows
                    self.elevate(rows)
                else:
                    direction = self.get_direction(target_face)

            elif self.facing_up == 'F':
                if target_face == 'F':
                    self.elevate(rows)
                elif use_shortcut and target_face == 'B':
                    rows = self.rows_and_cols - rows
                    self.elevate(rows)
                else:
                    direction = self.get_direction(target_face)

            elif self.facing_up == 'R':
                if target_face == 'R':
                    self.elevate(rows)
                elif use_shortcut and target_face == 'L':
                    rows = self.rows_and_cols - rows
                    self.elevate(rows)
                else:
                    direction = self.get_direction(target_face)

            elif self.facing_up == 'B':
                if target_face == 'B':
                    self.elevate(rows)
                elif use_shortcut and target_face == 'F':
                    rows = self.rows_and_cols - rows
                    self.elevate(rows)
                else:
                    direction = self.get_direction(target_face)

            elif self.facing_up == 'D':
                if target_face == 'D':
                    self.elevate(rows)
                elif use_shortcut and target_face == 'U':
                    rows = self.rows_and_cols - rows
                    self.elevate(rows)
                else:
                    direction = self.get_direction(target_face)

            else:
                raise Exception("Invalid face %s" % self.facing_up)

            if direction:
                if direction == 'north':
                    self.move_north_to_top(rows)
                elif direction == 'west':
                    self.move_west_to_top(rows)
                elif direction == 'south':
                    self.move_south_to_top(rows)
                elif direction == 'east':
                    self.move_east_to_top(rows)
                elif direction == 'down':
                    self.move_down_to_top(rows)
                else:
                    raise Exception("Unsupported direction %s" % direction)

            self.rotate(clockwise, quarter_turns)

            # Can we avoid this squish() call?  Doing this everytime really slows things down.
            # We can on smaller cubes but the big cubes jam up too easily.
            if self.rows_and_cols >= 6:
                self.elevate(rows=self.rows_and_cols)
                self.squish()

            # Every 25 moves make sure the squisher hasn't crept out of place
            if index % 25 == 0:
                self.squisher_reset()

            log.info("Up %s, Down %s, North %s, West %s, South %s, East %s" %
                    (self.facing_up, self.facing_down, self.facing_north, self.facing_west, self.facing_south, self.facing_east))
            log.info("\n\n\n\n")
            moves += 1
            #log.info("Paused")
            #input("Paused")

        finish = datetime.datetime.now()
        delta_ms = ((finish - start).seconds * 1000) + ((finish - start).microseconds / 1000)

        if moves:
            log.info("SOLVED!! %ds in elevate, %ds in flip, %ds in rotate, %ds in run_solution, %d moves, avg %dms per move" %
                (int(self.time_elevate/1000), int(self.time_flip/1000), int(self.time_rotate/1000),
                 int(delta_ms/1000), moves, int(delta_ms/moves)))

    def compress_actions(self, actions):
        actions = actions.replace("Uw Uw Uw ", "Uw' ")
        actions = actions.replace("Uw Uw ", "Uw2 ")
        actions = actions.replace("U U U ", "U' ")
        actions = actions.replace("U U ", "U2 ")

        actions = actions.replace("Lw Lw Lw ", "Lw' ")
        actions = actions.replace("Lw Lw ", "Lw2 ")
        actions = actions.replace("L L L ", "L' ")
        actions = actions.replace("L L ", "L2 ")

        actions = actions.replace("Fw Fw Fw ", "Fw' ")
        actions = actions.replace("Fw Fw ", "Fw2 ")
        actions = actions.replace("F F F ", "F' ")
        actions = actions.replace("F F ", "F2 ")

        actions = actions.replace("Rw Rw Rw ", "Rw' ")
        actions = actions.replace("Rw Rw ", "Rw2 ")
        actions = actions.replace("R R R ", "R' ")
        actions = actions.replace("R R ", "R2 ")

        actions = actions.replace("Bw Bw Bw ", "Bw' ")
        actions = actions.replace("Bw Bw ", "Bw2 ")
        actions = actions.replace("B B B ", "B' ")
        actions = actions.replace("B B ", "B2 ")

        actions = actions.replace("Dw Dw Dw ", "Dw' ")
        actions = actions.replace("Dw Dw ", "Dw2 ")
        actions = actions.replace("D D D ", "D' ")
        actions = actions.replace("D D ", "D2 ")
        return actions

    def resolve_actions(self):

        if self.shutdown_event.is_set():
            return

        if self.emulate:
            solution = """U2 3Uw B' 3Rw' 3Lw D 3Lw' D' 3Lw 3Fw2 D 3Bw' U' Lw' 3Fw2 U 3Rw2 Lw' D B' U' Lw' U Fw' Uw2 Bw 3Rw2 Fw R' 3Uw2 Fw' U Uw2 Bw' Uw2 L2 Rw' 3Dw 3Uw' D' 3Rw2 3Lw2 Fw' Rw' 3Uw2 3Bw2 3Dw' R' F 3Dw2 3Uw B' 3Dw' Uw' Dw F Uw' B 3Uw2 Dw' R Uw' 3Fw2 3Rw2 R' Bw2 F Uw' 3Bw2 3Fw2 B2 L' 3Dw2 3Uw2 B' R Dw' U Lw2 3Fw2 Bw2 Fw2 U' 3Bw2 3Rw2 Dw2 L' 3Uw2 Dw2 L 3Dw2 L' Dw2 R Uw2 L' 3Uw2 Uw2 F' 3Dw2 F' B 3Uw2 F 3Uw2 3Dw2 B 3Uw2 B' 3Uw2 Dw2 U2 Bw D2 U Bw Uw' Rw Uw Rw Lw2 Fw' Lw2 Uw' Fw2 Uw2 B' Uw L' Dw' U Uw2 Bw2 U L' Bw2 U' Uw2 L' Lw2 F' Uw2 3Lw2 F2 3Lw2 D 3Rw2 B2 3Rw2 F' R2 B' B2 3Lw2 D2 B2 3Rw2 D' R2 D' B2 L2 3Lw2 U' 3Rw U2 3Rw' B2 3Lw B2 D2 U2 3Rw 3Uw 3Dw2 B2 F2 L2 3Uw 3Dw2 B2 3Uw L2 3Dw' 3Bw' U2 3Bw2 U2 R2 U2 R2 3Fw' U2 3Bw' L2 3Fw U2 2Bw U' F' B2 U 2Bw' D L' L2 U2 2Rw2 B D2 B' D2 2Rw2 B' 2Bw' L2 R2 U2 2Bw' R2 U2 2Bw D2 2Fw' 2Lw B2 D2 U2 2Lw' D2 2Lw F2 2Rw' B2 2Rw2 2Dw2 F2 2Dw' F2 2Dw' F2 2Dw' R2 2Uw F2 2Uw' U' R F U' L2 U L2 B L' U2 R' U B2 D2 F2 B2 R2 U2 R2 U B2""".split()
            self.rows_and_cols = 7

        else:
            solution = send_command(self.SERVER, 10000, "GET_SOLUTION:%s" % self.cube_for_resolver).strip().split()

        self.run_solution(solution)
        self.elevate(0)
        self.squisher_reset()

        if not self.flipper_at_init:
            self.flip()

        # Rotate back to 0
        square_pos = round_to_quarter_turn(self.turntable.position)
        self._rotate(square_pos, True, False)

    def test_foo(self):
        foo = ("3Uw", )
        #foo = ("U'", )
        self.run_solution(foo)
        self.flip_to_init()
        self.elevate(0)

    def test_basics(self):
        """
        Test the three motors
        """

        input('Press ENTER to flip (to forward)')
        self.flip()

        if self.shutdown_event.is_set():
            return

        input('Press ENTER to flip (to init)')
        self.flip()

        if self.shutdown_event.is_set():
            return

        input('Press ENTER rotate 90 degrees clockwise')
        self.rotate(clockwise=True, quarter_turns=1)

        if self.shutdown_event.is_set():
            return

        input('Press ENTER rotate 90 degrees counter clockwise')
        self.rotate(clockwise=False, quarter_turns=1)

        if self.shutdown_event.is_set():
            return

        input('Press ENTER rotate 180 degrees clockwise')
        self.rotate(clockwise=True, quarter_turns=2)

        if self.shutdown_event.is_set():
            return

        input('Press ENTER rotate 180 degrees counter clockwise')
        self.rotate(clockwise=False, quarter_turns=2)

        if self.shutdown_event.is_set():
            return

        input('Press ENTER elevate to 1 rows')
        self.elevate(1)

        if self.shutdown_event.is_set():
            return

        input('Press ENTER elevate to lower')
        self.elevate(0)

        if self.shutdown_event.is_set():
            return

        input('Press ENTER elevate to 2 rows')
        self.elevate(2)

        if self.shutdown_event.is_set():
            return

        input('Press ENTER elevate to max rows')
        self.elevate_max()

        if self.shutdown_event.is_set():
            return

        input('Press ENTER elevate to 2 rows')
        self.elevate(2)

        if self.shutdown_event.is_set():
            return

        input('Press ENTER elevate to lower')
        self.elevate(0)

        if self.shutdown_event.is_set():
            return

        input('Press ENTER to rotate 1 row clockwise')
        self.elevate(1)
        self.rotate(clockwise=True, quarter_turns=1)

        if self.shutdown_event.is_set():
            return

        input('Press ENTER to rotate 1 row counter clockwise')
        self.elevate(1)
        self.rotate(clockwise=False, quarter_turns=1)

        if self.shutdown_event.is_set():
            return

        input('Press ENTER to rotate 2 row clockwise')
        self.elevate(2)
        self.rotate(clockwise=True, quarter_turns=1)

        if self.shutdown_event.is_set():
            return

        input('Press ENTER to rotate 2 row counter clockwise')
        self.elevate(2)
        self.rotate(clockwise=False, quarter_turns=1)

        if self.shutdown_event.is_set():
            return

        self.elevate(0)

    def test_patterns(self):
        """
        https://ruwix.com/the-rubiks-cube/rubiks-cube-patterns-algorithms/
        """
        # tetris = ("L", "R", "F", "B", "U’", "D’", "L’", "R’")
        checkerboard = ("F", "B2", "R’", "D2", "B", "R", "U", "D’", "R", "L’", "D’", "F’", "R2", "D", "F2", "B’")
        self.run_solution(checkerboard)


class CraneCuber2x2x2(CraneCuber3x3x3):

    def __init__(self, SERVER, emulate, rows_and_cols=2, size_mm=55):
        CraneCuber3x3x3.__init__(self, SERVER, emulate, rows_and_cols, size_mm)

        # This cube is so light it tends to get knocked around if we raise and lower it too fast
        # positive moves down
        # negative moves up
        self.ELEVATOR_SPEED_UP_FAST = 600
        self.ELEVATOR_SPEED_UP_SLOW = 600
        self.ELEVATOR_SPEED_DOWN_FAST = 600
        self.ELEVATOR_SPEED_DOWN_SLOW = 600

        # These are for a 40mm cube
        #self.TURN_BLOCKED_TOUCH_DEGREES = 77
        #self.TURN_BLOCKED_SQUARE_TT_DEGREES = 40
        #self.TURN_BLOCKED_SQUARE_CUBE_DEGREES = -117

        # These are for a 55mm cube
        self.TURN_BLOCKED_TOUCH_DEGREES = 270
        self.TURN_BLOCKED_SQUARE_TT_DEGREES = 80
        self.TURN_BLOCKED_SQUARE_CUBE_DEGREES = -390
        self.SQUISH_DEGREES = 140

        self.rows_in_turntable_to_count_as_face_turn = 2
        log.warning("Using CraneCuber2x2x2, rows_in_turntable_to_count_as_face_turn %d" % self.rows_in_turntable_to_count_as_face_turn)


class CraneCuber4x4x4(CraneCuber3x3x3):

    def __init__(self, SERVER, emulate, rows_and_cols=4, size_mm=62):
        CraneCuber3x3x3.__init__(self, SERVER, emulate, rows_and_cols, size_mm)

        # These are for a 62mm 4x4x4 cube
        self.TURN_BLOCKED_TOUCH_DEGREES = 116
        self.TURN_BLOCKED_SQUARE_TT_DEGREES = 40
        self.TURN_BLOCKED_SQUARE_CUBE_DEGREES = -154
        self.SQUISH_DEGREES = 100
        self.rows_in_turntable_to_count_as_face_turn = 4
        log.warning("Using CraneCuber4x4x4, rows_in_turntable_to_count_as_face_turn %d" % self.rows_in_turntable_to_count_as_face_turn)


class CraneCuber5x5x5(CraneCuber3x3x3):

    def __init__(self, SERVER, emulate, rows_and_cols=5, size_mm=63):
        CraneCuber3x3x3.__init__(self, SERVER, emulate, rows_and_cols, size_mm)

        # These are for a 63mm 5x5x5 cube
        self.TURN_BLOCKED_TOUCH_DEGREES = 100
        self.TURN_BLOCKED_SQUARE_TT_DEGREES = 30
        self.TURN_BLOCKED_SQUARE_CUBE_DEGREES = -140
        self.SQUISH_DEGREES = 90
        self.rows_in_turntable_to_count_as_face_turn = 3
        log.warning("Using CraneCuber5x5x5, rows_in_turntable_to_count_as_face_turn %d" % self.rows_in_turntable_to_count_as_face_turn)


class CraneCuber6x6x6(CraneCuber3x3x3):

    def __init__(self, SERVER, emulate, rows_and_cols=6, size_mm=67):
        CraneCuber3x3x3.__init__(self, SERVER, emulate, rows_and_cols, size_mm)

        # These are for a 67mm 6x6x6 cube
        self.TURN_BLOCKED_TOUCH_DEGREES = 68
        self.TURN_BLOCKED_SQUARE_TT_DEGREES = 26
        self.TURN_BLOCKED_SQUARE_CUBE_DEGREES = -90
        self.SQUISH_DEGREES = 60
        self.rows_in_turntable_to_count_as_face_turn = 6
        log.warning("Using CraneCuber6x6x6, rows_in_turntable_to_count_as_face_turn %d" % self.rows_in_turntable_to_count_as_face_turn)


class CraneCuber7x7x7(CraneCuber3x3x3):

    def __init__(self, SERVER, emulate, rows_and_cols=7, size_mm=69):
        CraneCuber3x3x3.__init__(self, SERVER, emulate, rows_and_cols, size_mm)

        # These are for a 69mm 7x7x7 cube
        self.TURN_BLOCKED_TOUCH_DEGREES = 46
        self.TURN_BLOCKED_SQUARE_TT_DEGREES = 26
        self.TURN_BLOCKED_SQUARE_CUBE_DEGREES = -90
        self.SQUISH_DEGREES = 55
        self.rows_in_turntable_to_count_as_face_turn = 4
        log.warning("Using CraneCuber7x7x7, rows_in_turntable_to_count_as_face_turn %d" % self.rows_in_turntable_to_count_as_face_turn)


class MonitorTouchSensor(Thread):

    def __init__(self, emulate):
        Thread.__init__(self)
        self.cc = None
        self.shutdown_event = Event()
        self.waiting_for_release = False

        if emulate:
            self.touch_sensor = DummySensor()
        else:
            self.touch_sensor = TouchSensor()

    def __str__(self):
        return "MonitorTouchSensor"

    def run(self):
        while True:

            if self.shutdown_event.is_set():
                log.warning('%s: shutdown_event is set' % self)
                break

            if self.touch_sensor.is_pressed:

                if not self.waiting_for_release:
                    self.waiting_for_release = True

                    if self.cc:
                        if self.cc.waiting_for_touch_sensor.is_set():
                            log.warning('%s: TouchSensor pressed, clearing cc waiting_for_touch_sensor' % self)
                            self.cc.waiting_for_touch_sensor.clear()
                        else:
                            log.warning('%s: TouchSensor pressed, setting cc shutdown_event' % self)
                            self.cc.mts = None
                            self.cc.shutdown_robot()
                            self.shutdown_event.set()
            else:
                if self.waiting_for_release:
                    self.waiting_for_release = False
                    log.warning('%s: TouchSensor released' % self)

            sleep(0.01)


if __name__ == '__main__':

    #logging.basicConfig(filename='/tmp/cranecuber.log',
    #                    level=logging.INFO,
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(filename)12s %(levelname)8s: %(message)s')
    log = logging.getLogger(__name__)

    # Color the errors and warnings in red
    logging.addLevelName(logging.ERROR, "\033[91m   %s\033[0m" % logging.getLevelName(logging.ERROR))
    logging.addLevelName(logging.WARNING, "\033[91m %s\033[0m" % logging.getLevelName(logging.WARNING))

    parser = argparse.ArgumentParser()
    parser.add_argument('--emulate', action='store_true', default=False, help='Run in emulator mode')
    args = parser.parse_args()

    server_conf = "server.conf"
    SERVER = None

    if os.path.exists(server_conf):

        with open(server_conf, 'r') as fh:
            for line in fh.readlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    SERVER = line
                    break

        if SERVER is None:
            print("ERROR: server.conf does not contain a server")
            sys.exit(1)
    else:
        SERVER = '0.0.0.0'

    send_command(SERVER, 10000, "PING")

    # Use this to test your TURN_BLOCKED_TOUCH_DEGREES
    '''
    #cc = CraneCuber2x2x2(SERVER, args.emulate)
    #cc = CraneCuber3x3x3(SERVER, args.emulate)
    #cc = CraneCuber4x4x4(SERVER, args.emulate)
    #cc = CraneCuber5x5x5(SERVER, args.emulate)
    cc = CraneCuber6x6x6(SERVER, args.emulate)
    #cc = CraneCuber7x7x7(SERVER, args.emulate)
    #cc.init_motors()
    #cc.test_foo()
    cc.squish()
    cc.shutdown_robot()
    sys.exit(0)
    '''

    # Uncomment to test something
    '''
    cc = CraneCuber4x4x4(SERVER, args.emulate)
    cc.init_motors()
    cc.move_down_to_top(1)

    # reset back to starting position
    cc.flip()
    cc.elevate(0)
    cc.shutdown_robot()
    sys.exit(0)
    '''

    cc = None
    mts = MonitorTouchSensor(args.emulate)
    mts.start()

    try:
        while True:

            # Use a CraneCuber6x6x6 object for scanning
            cc = CraneCuber6x6x6(SERVER, args.emulate)
            mts.cc = cc
            cc.mts = mts
            cc.init_motors()

            if not args.emulate:
                cc.waiting_for_touch_sensor.set()
                log.info('waiting for TouchSensor press')

            while not cc.shutdown_event.is_set() and cc.waiting_for_touch_sensor.is_set():
                sleep (0.1)

            if cc.shutdown_event.is_set():
                break

            cc.scan()
            cc.get_colors()

            # We have scanned all sides and know how many squares there are, use
            # this to create an object of the appropriate class
            #
            # cc.colors is a dict where the square_index is the key and the RGB is the value
            colors = deepcopy(cc.colors)
            squares_per_side = len(colors.keys()) / 6
            size = int(math.sqrt(squares_per_side))

            if size == 2:
                cc = CraneCuber2x2x2(SERVER, args.emulate)
            elif size == 3:
                cc = CraneCuber3x3x3(SERVER, args.emulate)
            elif size == 4:
                cc = CraneCuber4x4x4(SERVER, args.emulate)
            elif size == 5:
                cc = CraneCuber5x5x5(SERVER, args.emulate)
            elif size == 6:
                cc = CraneCuber6x6x6(SERVER, args.emulate)
            elif size == 7:
                pass
            else:
                raise Exception("%dx%dx%d cubes are not yet supported" % (size, size, size))

            mts.cc = cc
            cc.mts = mts
            cc.colors = colors
            cc.resolve_colors()
            cc.resolve_actions()

            if cc.shutdown_event.is_set() or args.emulate:
                break

        #log.info("%s: elevator moved %d degrees total (%d rotations)" % (cc.elevator, cc.elevator.total_distance, int(cc.elevator.total_distance/360)))
        #log.info("%s: turntable moved %d degrees total (%d quarter turns)" % (cc.turntable, cc.turntable.total_distance, int(cc.turntable.total_distance/TURNTABLE_TURN_DEGREES)))
        log.info("%d move_north_to_top_calls" % cc.move_north_to_top_calls)
        log.info("%d move_south_to_top_calls" % cc.move_south_to_top_calls)
        log.info("%d move_east_to_top_calls" % cc.move_east_to_top_calls)
        log.info("%d move_west_to_top_calls" % cc.move_west_to_top_calls)
        log.info("%d move_down_to_top_calls" % cc.move_down_to_top_calls)
        log.info("%d move_calls total" % (cc.move_north_to_top_calls + cc.move_south_to_top_calls + cc.move_east_to_top_calls + cc.move_west_to_top_calls + cc.move_down_to_top_calls))
        log.info("%d move_calls (east/west) total" % (cc.move_east_to_top_calls + cc.move_west_to_top_calls))
        cc.shutdown_robot()

    except Exception as e:
        log.exception(e)

        if mts:
            mts.shutdown_event.set()
            mts.join()
            mts = None

        if cc:
            cc.mts = None
            cc.shutdown_robot()

        sys.exit(1)
