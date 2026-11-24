#include <stdio.h>
#include <stdlib.h>
#define N 1000
int main(void) { size_t n = N; double *a = malloc(n*n*8), *b = malloc(n*n*8), *c = calloc(n*n, 8);
  for (size_t i = 0; i < n*n; i++) { a[i] = i % 7; b[i] = i % 5; }
  for (size_t i = 0; i < n; i++) for (size_t k = 0; k < n; k++) { double aik = a[i*n+k]; for (size_t j = 0; j < n; j++) c[i*n+j] += aik * b[k*n+j]; }
  double tr = 0; for (size_t i = 0; i < n; i++) tr += c[i*n+i]; printf("%.1f\n", tr); }
