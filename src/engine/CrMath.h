// Correctly rounded elementary functions (CORE-MATH, MIT licence, third_party/core-math).
// The engine uses these instead of the platform libm so that every platform (macOS,
// Windows UCRT, Linux glibc) computes exactly the same values: correctly rounded results
// are unique, and they reproduce the original's x87 results in all tested cases.
#pragma once

extern "C" {
double cr_exp(double);
double cr_log(double);
double cr_sin(double);
double cr_cos(double);
double cr_tan(double);
}

namespace sq8l::crmath {
inline double exp(double x) { return cr_exp(x); }
inline double log(double x) { return cr_log(x); }
inline double sin(double x) { return cr_sin(x); }
inline double cos(double x) { return cr_cos(x); }
inline double tan(double x) { return cr_tan(x); }
}  // namespace sq8l::crmath
