#include <stdio.h>
#include <stdint.h>
static uint32_t steps(uint64_t n) { uint32_t k = 0; while (n != 1 && k < 4000000000u) { if ((n & 1) == 0) n >>= 1; else n = 3*n + 1; k++; } return k; }
int main(void) { uint64_t best = 0, arg = 0; for (uint64_t i = 1; i < 5000000; i++) { uint64_t s = steps(i); if (s > best) { best = s; arg = i; } } printf("%llu %llu\n", (unsigned long long)arg, (unsigned long long)best); }
