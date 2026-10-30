module {
  func.func @add_constants() -> i32 {
    %a = arith.constant 20 : i32
    %b = arith.constant 22 : i32
    %sum = arith.addi %a, %b : i32
    return %sum : i32
  }
}
