#!/usr/bin/python
import sys
from decimal import *

def pi():
    """Compute Pi to the current precision.

    >>> print pi()
    3.141592653589793238462643383

    """
    getcontext().prec += 2  # extra digits for intermediate steps
    three = Decimal(3)      # substitute "three=3.0" for regular floats
    lasts, t, s, n, na, d, da = 0, three, 3, 1, 0, 0, 24
    while s != lasts:
        lasts = s
        n, na = n+na, na+8
        d, da = d+da, da+32
        t = (t * n) / d
        s += t
        print s
    getcontext().prec -= 2
    return +s               # unary plus applies the new precision

print sys.argv
if len(sys.argv) > 1:
	getcontext().prec = int(sys.argv[1])
print pi()
