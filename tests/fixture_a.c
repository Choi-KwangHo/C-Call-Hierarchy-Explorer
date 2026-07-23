#include "fixture_b.h"
#include <stdio.h>

static int double_value(int value)
{
    return value * 2;
}

int calculate(int input)
{
    /* ignored_call() must not enter the call graph. */
    printf("fake_call() in a string");
    return helper(double_value(input));
}

int recursive_count(int value)
{
    if (value <= 0) {
        return 0;
    }
    return recursive_count(value - 1);
}
