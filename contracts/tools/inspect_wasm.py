#!/usr/bin/env python3
"""Check contract wasm files against what the nearcore VM accepts, and write their hashes.

Usage:
    inspect_wasm.py [--protocol N] [--expect FILE:method,method] [--write-hashes DIR] FILE...

For every file this decodes all sections and every instruction (standard library only) and
checks the rules nearcore applies when it prepares a contract:

* wasm features: MVP, mutable globals, sign extension, saturating float-to-int, and from
  protocol 84 bulk memory and reference types. Multi-value, SIMD, threads, tail calls,
  exceptions, multi-memory, memory64, GC and typed function references are rejected.
* imports: only functions, only from module "env", only host functions nearcore knows, and
  each host function's own protocol gate.
* limits: contract size, functions, types, tables, table elements, globals, function body
  size, locals (also at most one per two bytes of code since protocol 87), params, blocks,
  and the peak operand-stack size of every function.

`--write-hashes DIR` writes DIR/SHA256SUMS (hex, `sha256sum -c` format) and DIR/CODE_HASHES
(base58 of the sha256, which is what RPC `view_account` reports as `code_hash`).
Exit status is 1 if any file breaks a rule.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------------------------
# nearcore facts (from runtime/near-vm-runner and core/parameters at nearcore 2.13.4 and master)
# ---------------------------------------------------------------------------------------------

BULK_MEMORY_PROTOCOL = 84  # reftypes_bulk_memory: { old: false, new: true } in 84.yaml

LIMITS = {
    "max_contract_size": 4_194_304,
    "max_functions_number_per_contract": 10_000,
    "max_locals_per_contract": 1_000_000,
    "max_tables_per_contract": 1,
    "max_elements_per_contract_table": 10_000,
    "max_function_body_size": 196_608,
    "max_types_per_contract": 1_024,
    "max_globals_per_contract": 65_536,
    "max_params_per_function": 64,
    "max_params_per_contract": 50_000,
    "max_blocks_per_function": 5_000,
    "max_blocks_per_contract": 50_000,
    "max_operand_stack_bytes_per_function": 8_192,
    "min_contract_size_per_local": 2,
    "initial_memory_bytes": 1_024 * 65_536,
}

# Host functions gated by a protocol feature, with the protocol version that enables them.
GATED_HOST_FUNCTIONS = {
    "promise_batch_action_deploy_global_contract": 78,
    "promise_batch_action_deploy_global_contract_by_account_id": 78,
    "promise_batch_action_use_global_contract": 78,
    "promise_batch_action_use_global_contract_by_account_id": 78,
    "chain_id": 85,
    "p256_verify": 85,
    "promise_batch_action_transfer_to_gas_key": 85,
    "promise_batch_action_add_gas_key_with_full_access": 85,
    "promise_batch_action_add_gas_key_with_function_call": 85,
    "promise_yield_create_with_id": 85,
    "promise_yield_resume_with_yield_id": 85,
    "ml_dsa_verify": 87,
    "sha3_256": 87,
    "sha3_384": 87,
    "sha3_512": 87,
    "universal_state_init_to_account_id": 88,
    "promise_batch_action_universal_state_init": 88,
}

HOST_FUNCTIONS = set(GATED_HOST_FUNCTIONS) | set(
    """
    read_register register_len write_register current_account_id signer_account_id
    signer_account_pk predecessor_account_id input block_index block_timestamp epoch_height
    storage_usage account_balance account_locked_balance attached_deposit prepaid_gas used_gas
    random_seed sha256 keccak256 keccak512 ed25519_verify ripemd160 ecrecover value_return panic
    panic_utf8 log_utf8 log_utf16 abort promise_create promise_then promise_and
    promise_batch_create promise_batch_then promise_set_refund_to promise_batch_action_state_init
    promise_batch_action_state_init_by_account_id set_state_init_data_entry current_contract_code
    refund_to_account_id promise_batch_action_create_account promise_batch_action_deploy_contract
    promise_batch_action_function_call promise_batch_action_function_call_weight
    promise_batch_action_transfer promise_batch_action_stake
    promise_batch_action_add_key_with_full_access promise_batch_action_add_key_with_function_call
    promise_batch_action_delete_key promise_batch_action_delete_account promise_yield_create
    promise_yield_resume promise_results_count promise_result promise_return storage_write
    storage_read storage_remove storage_has_key storage_iter_prefix storage_iter_range
    storage_iter_next validator_stake validator_total_stake alt_bn128_g1_multiexp alt_bn128_g1_sum
    alt_bn128_pairing_check bls12381_p1_sum bls12381_p2_sum bls12381_g1_multiexp
    bls12381_g2_multiexp bls12381_map_fp_to_g1 bls12381_map_fp2_to_g2 bls12381_pairing_check
    bls12381_p1_decompress bls12381_p2_decompress
    """.split()
)

# Value sizes nearcore uses when it measures the operand stack (SimpleMaxStackCfg).
I32, I64, F32, F64, V128, REF = 0x7F, 0x7E, 0x7D, 0x7C, 0x7B, 0x70
VALUE_SIZE = {I32: 4, I64: 8, F32: 4, F64: 8, V128: 16, REF: 8, 0x6F: 8}

# ---------------------------------------------------------------------------------------------
# Binary reader
# ---------------------------------------------------------------------------------------------


class WasmError(Exception):
    pass


class Reader:
    def __init__(self, data: bytes, pos: int = 0, end: int | None = None):
        self.data = data
        self.pos = pos
        self.end = len(data) if end is None else end

    def eof(self) -> bool:
        return self.pos >= self.end

    def byte(self) -> int:
        if self.pos >= self.end:
            raise WasmError("unexpected end of data")
        value = self.data[self.pos]
        self.pos += 1
        return value

    def bytes(self, count: int) -> bytes:
        if self.pos + count > self.end:
            raise WasmError("unexpected end of data")
        value = self.data[self.pos : self.pos + count]
        self.pos += count
        return value

    def u32(self) -> int:
        result = shift = 0
        while True:
            byte = self.byte()
            result |= (byte & 0x7F) << shift
            if byte < 0x80:
                return result
            shift += 7
            if shift > 35:
                raise WasmError("LEB128 too long")

    def sleb(self, bits: int) -> int:
        result = shift = 0
        while True:
            byte = self.byte()
            result |= (byte & 0x7F) << shift
            shift += 7
            if byte < 0x80:
                if shift < bits and byte & 0x40:
                    result -= 1 << shift
                return result
            if shift > bits + 7:
                raise WasmError("LEB128 too long")

    def name(self) -> str:
        return self.bytes(self.u32()).decode("utf-8")


# ---------------------------------------------------------------------------------------------
# Module model
# ---------------------------------------------------------------------------------------------


@dataclass
class Module:
    size: int
    types: list[tuple[list[int], list[int]]] = field(default_factory=list)
    imports: list[tuple[str, str, int]] = field(default_factory=list)  # (module, name, kind)
    imported_function_types: list[int] = field(default_factory=list)
    function_types: list[int] = field(default_factory=list)
    tables: list[int] = field(default_factory=list)  # initial sizes
    memories: int = 0
    memory_flags: list[int] = field(default_factory=list)
    globals: list[int] = field(default_factory=list)  # value types
    exports: dict[str, int] = field(default_factory=dict)  # name -> kind
    has_start: bool = False
    data_count: bool = False
    passive_segments: int = 0
    data_end: int = 0
    custom_sections: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    features: dict[str, int] = field(default_factory=dict)
    total_locals: int = 0
    max_body_size: int = 0
    max_function_blocks: int = 0
    total_blocks: int = 0
    max_stack_bytes: int = 0
    max_params: int = 0
    total_params: int = 0

    def use(self, feature: str) -> None:
        self.features[feature] = self.features.get(feature, 0) + 1

    def function_type(self, index: int) -> tuple[list[int], list[int]]:
        imported = len(self.imported_function_types)
        type_index = (
            self.imported_function_types[index]
            if index < imported
            else self.function_types[index - imported]
        )
        return self.types[type_index]


def read_value_type(reader: Reader, module: Module) -> int:
    value_type = reader.byte()
    if value_type == V128:
        module.use("simd")
    elif value_type in (0x70, 0x6F):
        module.use("reference-types")
    elif value_type not in (I32, I64, F32, F64):
        raise WasmError(f"unsupported value type 0x{value_type:02x} (GC or typed references)")
    return value_type


def read_table_type(reader: Reader, module: Module) -> None:
    """A funcref table is MVP; any other element type needs reference types."""
    if reader.data[reader.pos] == 0x70:
        reader.byte()
    else:
        read_value_type(reader, module)


def read_limits(reader: Reader, module: Module) -> tuple[int, int]:
    flags = reader.byte()
    if flags & 0x02:
        module.use("threads (shared memory)")
    if flags & 0x04:
        module.use("memory64")
    if flags & ~0x07:
        raise WasmError(f"unknown limits flags 0x{flags:02x}")
    minimum = reader.u32()
    if flags & 0x01:
        reader.u32()
    return flags, minimum


def skip_const_expr(reader: Reader, module: Module) -> int | None:
    """Skips an init expression. Returns its value when it is a plain i32.const."""
    value = None
    while True:
        opcode = reader.byte()
        if opcode == 0x0B:
            return value
        if opcode == 0x41:
            value = reader.sleb(32)
        elif opcode == 0x42:
            reader.sleb(64)
        elif opcode == 0x43:
            reader.bytes(4)
        elif opcode == 0x44:
            reader.bytes(8)
        elif opcode in (0x23, 0xD2):  # global.get, ref.func
            if opcode == 0xD2:
                module.use("reference-types")
            reader.u32()
        elif opcode == 0xD0:  # ref.null
            module.use("reference-types")
            reader.byte()
        elif opcode in (0x6A, 0x6B, 0x6C, 0x7C, 0x7D, 0x7E):
            module.use("extended-const")
        else:
            raise WasmError(f"unsupported opcode 0x{opcode:02x} in a constant expression")


def parse(data: bytes) -> Module:
    if data[:4] != b"\0asm" or data[4:8] != b"\x01\0\0\0":
        raise WasmError("not a wasm module (bad magic or version)")
    module = Module(size=len(data))
    reader = Reader(data, 8)
    while not reader.eof():
        section_id = reader.byte()
        size = reader.u32()
        body = Reader(data, reader.pos, reader.pos + size)
        reader.pos += size
        if section_id == 0:
            module.custom_sections.append(body.name())
        elif section_id == 1:
            for _ in range(body.u32()):
                form = body.byte()
                if form != 0x60:
                    raise WasmError(f"unsupported type form 0x{form:02x} (GC proposal)")
                params = [read_value_type(body, module) for _ in range(body.u32())]
                results = [read_value_type(body, module) for _ in range(body.u32())]
                if len(results) > 1:
                    module.use("multi-value")
                module.types.append((params, results))
        elif section_id == 2:
            for _ in range(body.u32()):
                module_name, name, kind = body.name(), body.name(), body.byte()
                module.imports.append((module_name, name, kind))
                if kind == 0:
                    module.imported_function_types.append(body.u32())
                elif kind == 1:
                    read_table_type(body, module)
                    read_limits(body, module)
                elif kind == 2:
                    read_limits(body, module)
                elif kind == 3:
                    read_value_type(body, module)
                    body.byte()
                else:
                    module.use("exceptions")
                    body.byte()
                    body.u32()
        elif section_id == 3:
            module.function_types = [body.u32() for _ in range(body.u32())]
        elif section_id == 4:
            for _ in range(body.u32()):
                read_table_type(body, module)
                module.tables.append(read_limits(body, module)[1])
        elif section_id == 5:
            module.memories = body.u32()
            for _ in range(module.memories):
                module.memory_flags.append(read_limits(body, module)[0])
        elif section_id == 6:
            for _ in range(body.u32()):
                module.globals.append(read_value_type(body, module))
                body.byte()
                skip_const_expr(body, module)
        elif section_id == 7:
            for _ in range(body.u32()):
                name, kind = body.name(), body.byte()
                body.u32()
                module.exports[name] = kind
        elif section_id == 8:
            module.has_start = True
        elif section_id == 9:
            parse_elements(body, module)
        elif section_id == 10:
            for index in range(body.u32()):
                body_size = body.u32()
                function = Reader(data, body.pos, body.pos + body_size)
                body.pos += body_size
                analyze_function(function, module, len(module.imported_function_types) + index)
                module.max_body_size = max(module.max_body_size, body_size)
        elif section_id == 11:
            parse_data(body, module)
        elif section_id == 12:
            module.data_count = True
            module.use("bulk-memory")
        elif section_id == 13:
            module.use("exceptions")
        else:
            raise WasmError(f"unknown section id {section_id}")
    return module


def parse_elements(body: Reader, module: Module) -> None:
    for _ in range(body.u32()):
        flags = body.u32()
        if flags not in (0, 2):
            module.use("bulk-memory")  # passive or declarative segments
        if flags & 0x01 == 0:
            if flags & 0x02:
                body.u32()
            skip_const_expr(body, module)
        if flags & 0x04:  # expressions
            if flags & 0x03:
                read_value_type(body, module)
            for _ in range(body.u32()):
                skip_const_expr(body, module)
        else:
            if flags & 0x03:
                body.byte()
            for _ in range(body.u32()):
                body.u32()


def parse_data(body: Reader, module: Module) -> None:
    for _ in range(body.u32()):
        flags = body.u32()
        offset = 0
        if flags == 1:
            module.passive_segments += 1
            module.use("bulk-memory")
        else:
            if flags == 2:
                memory = body.u32()
                if memory:
                    module.use("multi-memory")
            offset = skip_const_expr(body, module) or 0
        length = body.u32()
        body.bytes(length)
        if flags != 1:
            module.data_end = max(module.data_end, offset + length)


# ---------------------------------------------------------------------------------------------
# Code analysis
# ---------------------------------------------------------------------------------------------

# Numeric and conversion opcodes without immediates: opcode -> (pops, push type).
SIMPLE_OPS: dict[int, tuple[int, int | None]] = {}


def _simple(start: int, stop: int, pops: int, push: int) -> None:
    for opcode in range(start, stop + 1):
        SIMPLE_OPS[opcode] = (pops, push)


_simple(0x45, 0x45, 1, I32)
_simple(0x46, 0x4F, 2, I32)
_simple(0x50, 0x50, 1, I32)
_simple(0x51, 0x66, 2, I32)
_simple(0x67, 0x69, 1, I32)
_simple(0x6A, 0x78, 2, I32)
_simple(0x79, 0x7B, 1, I64)
_simple(0x7C, 0x8A, 2, I64)
_simple(0x8B, 0x91, 1, F32)
_simple(0x92, 0x98, 2, F32)
_simple(0x99, 0x9F, 1, F64)
_simple(0xA0, 0xA6, 2, F64)
_simple(0xA7, 0xAB, 1, I32)
_simple(0xAC, 0xB1, 1, I64)
_simple(0xB2, 0xB6, 1, F32)
_simple(0xB7, 0xBB, 1, F64)
_simple(0xBC, 0xBC, 1, I32)
_simple(0xBD, 0xBD, 1, I64)
_simple(0xBE, 0xBE, 1, F32)
_simple(0xBF, 0xBF, 1, F64)
_simple(0xC0, 0xC1, 1, I32)  # sign extension
_simple(0xC2, 0xC4, 1, I64)

LOAD_TYPES = {
    0x28: I32, 0x29: I64, 0x2A: F32, 0x2B: F64, 0x2C: I32, 0x2D: I32, 0x2E: I32, 0x2F: I32,
    0x30: I64, 0x31: I64, 0x32: I64, 0x33: I64, 0x34: I64, 0x35: I64,
}  # fmt: skip
STORE_OPS = range(0x36, 0x3F)


@dataclass
class Frame:
    base: int
    results: list[int]
    params: list[int]
    unreachable: bool = False


class Stack:
    """Operand stack of one function, tracking value sizes the way nearcore measures them."""

    def __init__(self) -> None:
        self.values: list[int] = []
        self.frames: list[Frame] = []
        self.bytes = 0
        self.peak = 0

    def push(self, value_type: int) -> None:
        self.values.append(value_type)
        self.bytes += VALUE_SIZE[value_type]
        self.peak = max(self.peak, self.bytes)

    def pop(self) -> int:
        frame = self.frames[-1]
        if len(self.values) <= frame.base:
            if frame.unreachable:
                return I64
            raise WasmError("operand stack underflow")
        value_type = self.values.pop()
        self.bytes -= VALUE_SIZE[value_type]
        return value_type

    def pops(self, count: int) -> None:
        for _ in range(count):
            self.pop()

    def truncate(self, height: int) -> None:
        while len(self.values) > height:
            self.bytes -= VALUE_SIZE[self.values.pop()]

    def enter(self, params: list[int], results: list[int]) -> None:
        self.pops(len(params))
        self.frames.append(Frame(len(self.values), results, params))
        for value_type in params:
            self.push(value_type)

    def mark_unreachable(self) -> None:
        frame = self.frames[-1]
        self.truncate(frame.base)
        frame.unreachable = True


def block_signature(reader: Reader, module: Module) -> tuple[list[int], list[int]]:
    first = reader.data[reader.pos]
    if first == 0x40:
        reader.byte()
        return [], []
    if first in VALUE_SIZE:
        return [], [read_value_type(reader, module)]
    type_index = reader.sleb(33)
    params, results = module.types[type_index]
    if params or len(results) > 1:
        module.use("multi-value")
    return params, results


def analyze_function(reader: Reader, module: Module, function_index: int) -> None:
    params, results = module.function_type(function_index)
    locals_: list[int] = list(params)
    for _ in range(reader.u32()):
        count = reader.u32()
        value_type = read_value_type(reader, module)
        module.total_locals += count
        if module.total_locals > LIMITS["max_locals_per_contract"]:
            raise WasmError("too many locals")
        locals_.extend([value_type] * count)
    module.max_params = max(module.max_params, len(params))
    module.total_params += len(params)

    stack = Stack()
    stack.frames.append(Frame(0, list(results), []))
    blocks = 0
    while not reader.eof():
        opcode = reader.byte()
        if opcode in SIMPLE_OPS:
            pops, push = SIMPLE_OPS[opcode]
            stack.pops(pops)
            stack.push(push)
            if 0xC0 <= opcode <= 0xC4:
                module.use("sign-extension")
        elif opcode in (0x02, 0x03, 0x04):  # block, loop, if
            blocks += 1
            block_params, block_results = block_signature(reader, module)
            if opcode == 0x04:
                stack.pop()
            stack.enter(block_params, block_results)
        elif opcode == 0x05:  # else
            frame = stack.frames[-1]
            stack.truncate(frame.base)
            frame.unreachable = False
            for value_type in frame.params:
                stack.push(value_type)
        elif opcode == 0x0B:  # end
            frame = stack.frames.pop()
            stack.truncate(frame.base)
            if not stack.frames:
                if not reader.eof():
                    raise WasmError("code after the end of a function")
                break
            for value_type in frame.results:
                stack.push(value_type)
        elif opcode in (0x00, 0x0F):  # unreachable, return
            stack.mark_unreachable()
        elif opcode == 0x01:  # nop
            pass
        elif opcode == 0x0C:  # br
            reader.u32()
            stack.mark_unreachable()
        elif opcode == 0x0D:  # br_if
            reader.u32()
            stack.pop()
        elif opcode == 0x0E:  # br_table
            for _ in range(reader.u32() + 1):
                reader.u32()
            stack.pop()
            stack.mark_unreachable()
        elif opcode == 0x10:  # call
            callee_params, callee_results = module.function_type(reader.u32())
            stack.pops(len(callee_params))
            for value_type in callee_results:
                stack.push(value_type)
        elif opcode == 0x11:  # call_indirect
            callee_params, callee_results = module.types[reader.u32()]
            if reader.u32() != 0:
                module.use("reference-types")
            stack.pop()
            stack.pops(len(callee_params))
            for value_type in callee_results:
                stack.push(value_type)
        elif opcode in (0x12, 0x13):
            module.use("tail calls")
            raise WasmError("tail calls are not supported by nearcore")
        elif opcode in (0x06, 0x07, 0x08, 0x09, 0x0A, 0x18, 0x19, 0x1F):
            module.use("exceptions")
            raise WasmError("exception handling is not supported by nearcore")
        elif opcode in (0x14, 0x15, 0xD3, 0xD4, 0xD5, 0xD6):
            module.use("typed function references")
            raise WasmError("typed function references are not supported by nearcore")
        elif opcode == 0x1A:  # drop
            stack.pop()
        elif opcode in (0x1B, 0x1C):  # select
            if opcode == 0x1C:
                module.use("reference-types")
                for _ in range(reader.u32()):
                    read_value_type(reader, module)
            stack.pop()
            stack.pop()
            stack.push(stack.pop())
        elif opcode == 0x20:
            stack.push(locals_[reader.u32()])
        elif opcode == 0x21:
            reader.u32()
            stack.pop()
        elif opcode == 0x22:
            stack.pop()
            stack.push(locals_[reader.u32()])
        elif opcode == 0x23:
            index = reader.u32()
            imported = [kind for (_, _, kind) in module.imports if kind == 3]
            stack.push(I32 if index < len(imported) else module.globals[index - len(imported)])
        elif opcode == 0x24:
            reader.u32()
            stack.pop()
        elif opcode in (0x25, 0x26):  # table.get / table.set
            module.use("reference-types")
            reader.u32()
            if opcode == 0x25:
                stack.pop()
                stack.push(REF)
            else:
                stack.pops(2)
        elif opcode in LOAD_TYPES or opcode in STORE_OPS:
            align = reader.u32()
            if align & 0x40:
                module.use("multi-memory")
                reader.u32()
            reader.u32()  # offset
            if opcode in LOAD_TYPES:
                stack.pop()
                stack.push(LOAD_TYPES[opcode])
            else:
                stack.pops(2)
        elif opcode in (0x3F, 0x40):  # memory.size, memory.grow
            if reader.u32() != 0:
                module.use("multi-memory")
            if opcode == 0x40:
                stack.pop()
            stack.push(I32)
        elif opcode == 0x41:
            reader.sleb(32)
            stack.push(I32)
        elif opcode == 0x42:
            reader.sleb(64)
            stack.push(I64)
        elif opcode == 0x43:
            reader.bytes(4)
            stack.push(F32)
        elif opcode == 0x44:
            reader.bytes(8)
            stack.push(F64)
        elif opcode == 0xD0:  # ref.null
            module.use("reference-types")
            reader.byte()
            stack.push(REF)
        elif opcode == 0xD1:  # ref.is_null
            module.use("reference-types")
            stack.pop()
            stack.push(I32)
        elif opcode == 0xD2:  # ref.func
            module.use("reference-types")
            reader.u32()
            stack.push(REF)
        elif opcode == 0xFC:
            analyze_misc(reader, module, stack)
        elif opcode == 0xFD:
            module.use("simd")
            raise WasmError("SIMD is not supported by nearcore")
        elif opcode == 0xFE:
            module.use("threads (atomics)")
            raise WasmError("atomics are not supported by nearcore")
        elif opcode == 0xFB:
            module.use("gc")
            raise WasmError("GC instructions are not supported by nearcore")
        else:
            raise WasmError(f"unknown opcode 0x{opcode:02x}")
    module.max_function_blocks = max(module.max_function_blocks, blocks)
    module.total_blocks += blocks
    module.max_stack_bytes = max(module.max_stack_bytes, stack.peak)


def analyze_misc(reader: Reader, module: Module, stack: Stack) -> None:
    sub = reader.u32()
    if sub <= 7:  # saturating float-to-int
        module.use("saturating-float-to-int")
        stack.pop()
        stack.push(I32 if sub <= 3 else I64)
    elif sub in (8, 10, 11, 12, 14):  # memory.init, memory.copy, memory.fill, table.init, table.copy
        module.use("bulk-memory")
        immediates = {8: 2, 10: 2, 11: 1, 12: 2, 14: 2}[sub]
        indexes = [reader.u32() for _ in range(immediates)]
        if sub in (8, 10, 11) and any(indexes[1:] if sub == 8 else indexes):
            module.use("multi-memory")
        stack.pops(3)
    elif sub in (9, 13):  # data.drop, elem.drop
        module.use("bulk-memory")
        reader.u32()
    elif sub in (15, 16, 17):  # table.grow, table.size, table.fill
        module.use("reference-types")
        reader.u32()
        if sub == 15:
            stack.pops(2)
            stack.push(I32)
        elif sub == 16:
            stack.push(I32)
        else:
            stack.pops(3)
    else:
        module.use(f"misc opcode 0xFC {sub}")
        raise WasmError(f"0xFC {sub} (wide arithmetic or newer) is not supported by nearcore")


# ---------------------------------------------------------------------------------------------
# Checks and report
# ---------------------------------------------------------------------------------------------

ALLOWED_FEATURES = {"sign-extension", "saturating-float-to-int", "bulk-memory", "reference-types"}


Row = tuple[str, int, int]


def check(module: Module, protocol: int, expected_exports: list[str]) -> tuple[list[str], int, list[Row]]:
    """Returns (problems, minimum protocol version the module needs, measured limits)."""
    problems = list(module.problems)
    needed = 0
    for feature in module.features:
        if feature not in ALLOWED_FEATURES:
            problems.append(f"uses {feature}, which nearcore rejects")
    if {"bulk-memory", "reference-types"} & module.features.keys():
        needed = BULK_MEMORY_PROTOCOL

    for module_name, name, kind in module.imports:
        if kind != 0:
            problems.append(f"imports a non-function {module_name}.{name}")
        elif module_name != "env":
            problems.append(f"imports {module_name}.{name}; only the env module is allowed")
        elif name not in HOST_FUNCTIONS:
            problems.append(f"imports unknown host function env.{name}")
        else:
            needed = max(needed, GATED_HOST_FUNCTIONS.get(name, 0))
    if needed > protocol:
        problems.append(f"needs protocol {needed} but the target is {protocol}")

    functions = len(module.imported_function_types) + len(module.function_types)
    limits: list[Row] = [
        ("contract size", module.size, LIMITS["max_contract_size"]),
        ("functions", functions, LIMITS["max_functions_number_per_contract"]),
        ("types", len(module.types), LIMITS["max_types_per_contract"]),
        ("tables", len(module.tables), LIMITS["max_tables_per_contract"]),
        ("table elements", max(module.tables, default=0), LIMITS["max_elements_per_contract_table"]),
        ("globals", len(module.globals), LIMITS["max_globals_per_contract"]),
        ("largest function body", module.max_body_size, LIMITS["max_function_body_size"]),
        (
            "locals",
            module.total_locals,
            min(LIMITS["max_locals_per_contract"], module.size // LIMITS["min_contract_size_per_local"]),
        ),
        ("params of one function", module.max_params, LIMITS["max_params_per_function"]),
        ("params of all functions", module.total_params, LIMITS["max_params_per_contract"]),
        ("blocks in one function", module.max_function_blocks, LIMITS["max_blocks_per_function"]),
        ("blocks in all functions", module.total_blocks, LIMITS["max_blocks_per_contract"]),
        ("peak operand stack bytes", module.max_stack_bytes, LIMITS["max_operand_stack_bytes_per_function"]),
        ("initialized memory bytes", module.data_end, LIMITS["initial_memory_bytes"]),
    ]
    for label, value, limit in limits:
        if value > limit:
            problems.append(f"{label}: {value} exceeds nearcore's limit {limit}")
    if module.memories > 1:
        problems.append("declares more than one memory")
    if module.has_start:
        problems.append("has a start function, which would run on every call")
    for name in expected_exports:
        if module.exports.get(name) != 0:
            problems.append(f"does not export method {name}")
    return problems, needed, limits


def base58(data: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = int.from_bytes(data, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = alphabet[remainder] + encoded
    leading_zeros = len(data) - len(data.lstrip(b"\0"))
    return "1" * leading_zeros + encoded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--protocol", type=int, default=86, help="target protocol version (default 86)")
    parser.add_argument(
        "--expect",
        action="append",
        default=[],
        metavar="FILE:METHODS",
        help="file name and comma-separated methods it must export (repeatable)",
    )
    parser.add_argument("--write-hashes", type=Path, help="directory for SHA256SUMS and CODE_HASHES")
    args = parser.parse_args(argv)
    expected: dict[str, list[str]] = {}
    for item in args.expect:
        file_name, _, methods = item.partition(":")
        expected[file_name] = [name for name in methods.split(",") if name]

    failed = False
    sums, code_hashes = [], []
    for path in args.files:
        data = path.read_bytes()
        digest = hashlib.sha256(data)
        sums.append(f"{digest.hexdigest()}  {path.name}")
        code_hashes.append(f"{base58(digest.digest())}  {path.name}")
        try:
            module = parse(data)
            problems, needed, limits = check(module, args.protocol, expected.get(path.name, []))
        except WasmError as error:
            print(f"{path}: cannot be prepared by nearcore: {error}")
            failed = True
            continue
        methods = sorted(name for name, kind in module.exports.items() if kind == 0)
        print(f"{path} ({len(data)} bytes)")
        print(f"  sha256 {digest.hexdigest()}  code_hash {base58(digest.digest())}")
        print(f"  features: {', '.join(f'{k} x{v}' for k, v in sorted(module.features.items())) or 'MVP only'}")
        print(f"  imports: {len(module.imports)} host functions from env; needs protocol >= {needed}")
        for label, value, limit in limits:
            print(f"  {label}: {value} (limit {limit})")
        print(f"  exported methods ({len(methods)}): {' '.join(methods)}")
        print(f"  custom sections: {', '.join(module.custom_sections) or 'none'}")
        if problems:
            failed = True
            for problem in problems:
                print(f"  PROBLEM: {problem}")
        else:
            print(f"  OK for nearcore at protocol {args.protocol}")

    if args.write_hashes:
        args.write_hashes.mkdir(parents=True, exist_ok=True)
        (args.write_hashes / "SHA256SUMS").write_text("\n".join(sums) + "\n")
        (args.write_hashes / "CODE_HASHES").write_text("\n".join(code_hashes) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
