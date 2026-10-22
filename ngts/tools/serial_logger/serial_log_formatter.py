"""
Based on:
    sx_fit_regression/libs/scripts/logs_extractor/serial_log_formatter.py
"""

import argparse
import sys
import re
from collections import deque
from datetime import datetime

ANSI_ESCAPE = re.compile(r'(\x9B|\x1B\[)[0-?]*[ -\/]*[@-~]|\x1B\][^\x07]*\x07|\x1B[()].|\x1B[^\[]')
LONG_SPACE = re.compile(r'\s{5,}')
# kernel "[  428.372706] " / BMC "[00:00:00.386,595] " - a real reprint gets a new stamp
STAMPED_LINE = re.compile(r'\[[\d\s.:,]+\] ')


def parse_args():
    cmd_line_parser = argparse.ArgumentParser(usage=__doc__,
                                              formatter_class=argparse.RawDescriptionHelpFormatter)
    cmd_line_parser.add_argument('-b', '--before', help='string to be inserted as before each log line',
                                 required=False, dest="before", default="")
    cmd_line_parser.add_argument('-a', '--after', help='string to be inserted as after each log line',
                                 required=False, dest="after", default="")
    args = cmd_line_parser.parse_args()
    if args.before:
        args.before = args.before.strip() + " "
    if args.after:
        args.after = " " + args.after.strip()
    return args


def main(parsed_args):
    """read lines from stdin, remove ANSI escape chars and long spaces, format log lines before writing to stdout"""
    recent_stamped_lines = deque(maxlen=200)  # ponytail: > any screen redraw height
    # Receive lines from pipe stream
    input_line = sys.stdin.readline()
    while input_line:
        # Remove ASCII escape sequences
        input_line = ANSI_ESCAPE.sub('', input_line)
        # Replace long spaces with newline
        input_line = LONG_SPACE.sub('\n', input_line)
        for line in input_line.splitlines():
            line = line.strip()
            if STAMPED_LINE.match(line):
                # screen-based consoles (lab console hosts) repaint printed rows, whole or cut at the window width
                if any(printed.startswith(line) for printed in recent_stamped_lines):
                    continue
                recent_stamped_lines.append(line)
            if len(line):
                # Add the current time to the line and print it
                sys.stdout.writelines(datetime.now().strftime("%b %d %H:%M:%S") + " " + parsed_args.before + line +
                                      parsed_args.after + "\n")
                sys.stdout.flush()
        # Read the next line
        input_line = sys.stdin.readline()


if __name__ == "__main__":
    main(parse_args())
