# Copyright 2004-2025 Tom Rothamel <pytom@bishoujo.us>
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

# pyright: reportImportCycles=false

"""
This module is defined to allow us to program in Python 2 with a high degree
of compatibility with Python 3, and vice versa. It's intended to be invoked
with the following preamble::

    from __future__ import division, absolute_import, with_statement, print_function, unicode_literals
    from renpy.compat import *

Right now, it does the following things:

* Sets up aliases for Python 3 module moves, allowing the Python 3 names
  to be used in Python 2.

* Defines PY2 in the current context, to make Python 2 conditional.

* Aliases pickle to cPickle on Python 3, to support Python 2 code
  choosing between the implementations, where the choice is meaningful

* Replaces open with a function that mimics the Python 3 behavior, of
  opening files in a unicode-friendly mode by default.

* Redefines the text types, so that str is always the unicode type, and
  basestring is the list of string types available on the system.

* Exposes bchr, bord, and tobytes from future.utils.

* Changes the meaning of the .items(), .keys(), and .values() methods of
  dict to return views, rather than lists. (This is a fairly major change,
  and so is only available when with_statement and division are both
  imported.

* Aliases xrange to range on Python 2.

* Changes the behavior of TextIOWrapper.write so that bytes strings are promoted
  to unicode strings before being written.
"""

from __future__ import (
    division,
    absolute_import,
    with_statement,
    print_function,
    unicode_literals,
)

import builtins

from collections.abc import Iterable
import io
import sys
import operator


################################################################################
# Determine if this is Python2.

PY2 = False

################################################################################
# Make open mimic Python 3.

python_open = open
open = builtins.open
compat_open = open


################################################################################
# Codecs.

import codecs

strict_error = codecs.lookup_error("strict")
codecs.register_error("python_strict", strict_error)


################################################################################
# String (text and binary) types and functions.

basestring = (str,)
pystr = str
unicode = str
str = builtins.str


def bord(s: bytes) -> int:
    return s[0]


def bchr(i: int) -> bytes:
    return bytes([i])


def tobytes(s: str | bytes | Iterable[int]) -> bytes:
    if isinstance(s, bytes):
        return s
    else:
        if isinstance(s, str):
            return s.encode("latin-1")
        else:
            return bytes(s)


chr = builtins.chr

################################################################################
# Range.

range = builtins.range

################################################################################
# Round.

round = builtins.round

################################################################################
# Export functions.

__all__ = [
    "PY2",
    "open",
    "basestring",
    "str",
    "pystr",
    "range",
    "round",
    "bord",
    "bchr",
    "tobytes",
    "chr",
    "unicode",
]


# Generated by scripts/relative_imports.py, do not edit below this line.
import typing

if typing.TYPE_CHECKING:
    from . import fixes as fixes
    from . import pickle as pickle
