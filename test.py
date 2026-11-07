#!/usr/bin/env python3
# -*- coding: utf-8 -*-

def sum_1_to_n(n):
    """1부터 n 까지의 합을 계산합니다."""
    return sum(range(1, n + 1))


if __name__ == "__main__":
    # 1 부터 100 까지의 합 계산
    result = sum_1_to_n(100)
    
    print(f"1 ~ 100 까지의 합: {result}")
    
    # 수학적 공식으로 검증 (n=100일 때)
    formula_result = (1 + 100) * 100 / 2
    print(f"수학적 공식 결과: {formula_result}")
    
    print(f"\n결과가 일치합니다: {result == formula_result}")
