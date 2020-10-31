#! /usr/bin/python3

import argparse
import importlib
import os
import re
from pathlib import Path
import segtypes
import sys
import yaml
from collections import OrderedDict
from segtypes.segment import parse_segment_type
from segtypes.code import N64SegCode

parser = argparse.ArgumentParser(description="Split a rom given a rom, a config, and output directory")
parser.add_argument("rom", help="path to a .z64 rom")
parser.add_argument("config", help="path to a compatible config .yaml file")
parser.add_argument("outdir", help="a directory in which to extract the rom")
parser.add_argument("--modes", nargs="+", default="all")
parser.add_argument("--verbose", action="store_true", help="Enable debug logging")


def write_ldscript(rom_name, repo_path, sections):
    with open(os.path.join(repo_path, rom_name + ".ld"), "w", newline="\n") as f:
        f.write(
            "SECTIONS\n"
            "{\n"
        )
        f.write("\n    ".join(s.replace("\n", "\n    ") for s in sections))
        f.write(
            "\n"
            "}\n"
        )


def write_ld_addrs_h(repo_path, h_path, symbols):
    with open(os.path.join(repo_path, h_path), "w") as f:
        f.write(
            "#ifndef _SPLAT_LD_ADDRS_H_\n"
            "#define _SPLAT_LD_ADDRS_H_\n"
            "\n"
        )
        for symbol, addr in symbols.items():
            f.write("extern void* ")
            f.write(symbol)
            f.write(";\n")

            f.write("#define LD_")
            f.write(symbol)
            f.write(" ")
            f.write(f"0x{addr:X}")
            f.write("\n")

            f.write("\n")
        f.write(
            "#endif\n"
        )

def parse_file_start(split_file):
    return split_file[0] if "start" not in split_file else split_file["start"]


def gather_c_funcs(repo_path):
    funcs = {}
    labels_to_add = set()

    funcs_path = os.path.join(repo_path, "include", "functions.h")
    if os.path.exists(funcs_path):
        with open(funcs_path) as f:
            func_lines = f.readlines()

        for line in func_lines:
            if line.startswith("/* 0x"):
                line_split = line.strip().split(" ")
                addr_comment = line_split[1]
                addr = int(addr_comment[:10], 0)
                name = line_split[4][:line_split[4].find("(")]

                # We need to add marked functions' glabels in asm
                if len(addr_comment) > 10 and addr_comment[10] == '!':
                    labels_to_add.add(name)

                funcs[addr] = name

    # Manual list of func name / addrs
    func_addrs_path = os.path.join(repo_path, "tools", "symbol_addrs.txt")
    if os.path.exists(func_addrs_path):
        with open(func_addrs_path) as f:
            func_addrs_lines = f.readlines()

        for line in func_addrs_lines:
            line_split = line.strip().split(";")
            name = line_split[0]
            if name.startswith("!"):
                name = name[1:]
                labels_to_add.add(name)

            addr = int(line_split[1], 0)
            funcs[addr] = name

    return funcs, labels_to_add


def gather_c_variables(repo_path):
    vars = {}

    vars_path = os.path.join(repo_path, "include", "variables.h")
    if os.path.exists(vars_path):
        with open(vars_path) as f:
            vars_lines = f.readlines()

        for line in vars_lines:
            if line.startswith("/* 0x"):
                line_split = line.strip().split(" ")
                addr_comment = line_split[1]
                addr = int(addr_comment, 0)

                name = line_split[-1][:re.search(r'[\\[;]', line_split[-1]).start()]

                vars[addr] = name

    undefined_syms_path = os.path.join(repo_path, "undefined_syms.txt")
    if os.path.exists(undefined_syms_path):
        with open(undefined_syms_path) as f:
            us_lines = f.readlines()

        for line in us_lines:
            line = line.strip()
            if not line == "" and not line.startswith("//"):
                line_split = line.split("=")
                name = line_split[0].strip()
                addr = int(line_split[1].strip()[:-1], 0)
                vars[addr] = name

    return vars


def main(rom_path, config_path, repo_path, modes, verbose):
    with open(rom_path, "rb") as f:
        rom_bytes = f.read()

    # Create main output dir
    Path(repo_path).mkdir(parents=True, exist_ok=True)

    # Load config
    with open(config_path) as f:
        config = yaml.safe_load(f.read())

    options = config.get("options")
    options["modes"] = modes
    options["verbose"] = verbose

    c_funcs, c_func_labels_to_add = gather_c_funcs(repo_path)
    c_vars = gather_c_variables(repo_path)

    segments = []
    ld_sections = []
    ld_symbols = OrderedDict()
    seen_segment_names = set()

    defined_funcs = set()
    undefined_funcs = set()

    # Initialize segments
    for i, segment in enumerate(config['segments']):
        if len(segment) == 1:
            # We're at the end
            continue

        seg_type = parse_segment_type(segment)

        segmodule = importlib.import_module("segtypes." + seg_type)
        segment_class = getattr(segmodule, "N64Seg" + seg_type[0].upper() + seg_type[1:])

        segment = segment_class(segment, config['segments'][i + 1], options)
        segments.append(segment)

        if segment_class.require_unique_name:
            if segment.name in seen_segment_names:
                print(f"ERROR: Segment name {segment.name} is not unique")
                exit(1)
            seen_segment_names.add(segment.name)

        if type(segment) == N64SegCode:
            segment.all_functions = defined_funcs
            segment.c_functions = c_funcs
            segment.c_variables = c_vars
            segment.c_labels_to_add = c_func_labels_to_add

        if verbose:
            print(f"Splitting {segment.type} {segment.name} at 0x{segment.rom_start:X}")

        segment.check()
        segment.split(rom_bytes, repo_path)

        if type(segment) == N64SegCode:
            defined_funcs |= segment.glabels_added
            undefined_funcs |= segment.glabels_to_add

        ld_section, seg_ld_symbols = segment.get_ld_section()

        for symbol, addr in seg_ld_symbols.items():
            ld_section += f"{symbol} = 0x{addr:X};\n"

        ld_sections.append(ld_section)
        ld_symbols.update(seg_ld_symbols)

    for segment in segments:
        segment.postsplit(segments)

    # Write ldscript
    if "ld" in options["modes"] or "all" in options["modes"]:
        write_ldscript(config['basename'], repo_path, ld_sections)

        if "ld_addrs_header" in options:
            write_ld_addrs_h(repo_path, options["ld_addrs_header"], ld_symbols)

    # Write undefined_funcs.txt
    c_predefined_funcs = set(c_funcs.keys())
    to_write = sorted(undefined_funcs - defined_funcs - c_predefined_funcs)
    if len(to_write) > 0:
        with open(os.path.join(repo_path, "undefined_funcs.txt"), "w", newline="\n") as f:
            for line in to_write:
                f.write(line + " = 0x" + line[5:13].upper() + ";\n")


if __name__ == "__main__":
    args = parser.parse_args()
    main(args.rom, args.config, args.outdir, args.modes, args.verbose)