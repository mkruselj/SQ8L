/* C23 roundeven() for C runtimes that lack it (MinGW/UCRT): round to nearest integer,
   ties to even, independent of the current rounding mode. x - trunc(x) is exact. */
#include <math.h>

double roundeven(double x) {
    if (!(fabs(x) < 4503599627370496.0)) return x; /* |x| >= 2^52, inf or NaN: already integral */
    double t = trunc(x);
    double d = fabs(x - t);
    if (d > 0.5 || (d == 0.5 && fmod(t, 2.0) != 0.0)) t += copysign(1.0, x);
    return t;
}
