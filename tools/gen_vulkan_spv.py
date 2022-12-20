#!/usr/bin/env python3

import argparse
import array
import copy
import glob
import os
import re
import sys
import subprocess
import yaml
from collections import OrderedDict
from torchgen.code_template import CodeTemplate
from dataclasses import dataclass
from typing import List
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader  # type: ignore[misc]

H_NAME = "spv.h"
CPP_NAME = "spv.cpp"
DEFAULT_ENV = {"precision": "highp", "format": "rgba32f"}

# https://gist.github.com/pypt/94d747fe5180851196eb
class UniqueKeyLoader(Loader):
    def construct_mapping(self, node, deep=False):  # type: ignore[no-untyped-def]
        if not isinstance(node, MappingNode):
            raise ConstructorError(
                None,
                None,
                "expected a mapping node, but found %s" % node.id,
                node.start_mark,
            )
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)  # type: ignore[no-untyped-call]
            try:
                hash(key)
            except TypeError as e:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found unacceptable key ",
                    key_node.start_mark,
                ) from e
            # check for duplicate keys
            if key in mapping:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found duplicate key",
                    key_node.start_mark,
                )
            value = self.construct_object(value_node, deep=deep)  # type: ignore[no-untyped-call]
            mapping[key] = value
        return mapping


class VulkanShaderGenerator(object):
    standard_header = """
#version 450 core
#define PRECISION $precision
#define FORMAT $format

"""

    def __init__(self):  # type: ignore[no-untyped-def]
        self.ops_template_params = {}

    def add_params_yaml(self, parameters_yaml_file):  # type: ignore[no-untyped-def]
        all_template_params = OrderedDict()
        with open(parameters_yaml_file, "r") as f:
            contents = yaml.load(f, Loader=UniqueKeyLoader)
            for key in contents:
                all_template_params[key] = contents[key]
        self.validate_and_construct_op_params(all_template_params)  # type: ignore[no-untyped-call]

    def validate_and_construct_op_params(self, all_template_params):  # type: ignore[no-untyped-def]
        for op in all_template_params:
            if op in self.ops_template_params:
                raise KeyError(f"{op} params file has already been parsed")
            op_params_default_vals = all_template_params[op][
                "parameter_names_with_default_values"
            ]
            template_params_set = set(op_params_default_vals.keys())
            self.ops_template_params[op] = []
            self.ops_template_params[op].append(op_params_default_vals)
            op_template_params_values = all_template_params[op]["parameter_values"]
            for param_vals in op_template_params_values:
                param_vals_set = set(param_vals.keys())
                missing_keys = template_params_set - param_vals_set
                invalid_keys = param_vals_set - template_params_set
                if (len(invalid_keys)) > 0:
                    raise KeyError(f"Invalid keys {invalid_keys} are found")
                param_vals_copy = copy.deepcopy(op_params_default_vals)
                for key in param_vals:
                    param_vals_copy[key] = param_vals[key]
                self.ops_template_params[op].append(param_vals_copy)

    def generate(self, glsl_template_in, out_dir):  # type: ignore[no-untyped-def]
        glsl_template_name = os.path.basename(glsl_template_in)
        op_name, extension_name = glsl_template_name.split(".")
        if extension_name != "glslt":
            raise TypeError(f"invalid file type for glsl template {extension_name}")
        if op_name not in self.ops_template_params:
            raise KeyError(f"{op_name} params have not been populated")
        code_template = CodeTemplate.from_file(glsl_template_in)
        for template_params in self.ops_template_params[op_name]:
            content = VulkanShaderGenerator.standard_header
            param_vals_string = "x".join([str(i) for i in template_params.values()])
            output_file_name = op_name + "_" + param_vals_string + ".glsl"
            content += code_template.substitute(template_params)
            output_file = os.path.join(out_dir, output_file_name)
            with open(output_file, "w") as f:
                f.write(content)

@dataclass
class ShaderInfo:
    tile_size: List[int]
    layouts: List[str]
    weight_storage_type: str = ""
    bias_storage_type: str = ""

def getName(filePath):
    return os.path.basename(filePath).replace("/", "_").replace(".", "_")

def isDescriptorLine(lineStr):
    descriptorLineId = r"^layout\(set"
    return re.search(descriptorLineId, lineStr)

def isTileSizeLine(lineStr):
    tile_size_id = r"^ \* TILE_SIZE = \("
    return re.search(tile_size_id, lineStr)

def findTileSizes(lineStr):
    tile_size_id = r"^ \* TILE_SIZE = \(([0-9]+), ([0-9]+), ([0-9]+)\)"
    matches = re.search(tile_size_id, lineStr)
    return [int(matches.group(1)), int(matches.group(2)), int(matches.group(3))]

def isWeightStorageTypeLine(lineStr):
    weight_storage_id = r"^ \* WEIGHT_STORAGE = "
    return re.search(weight_storage_id, lineStr)

def getWeightStorageType(lineStr):
    weight_storage_id = r"^ \* WEIGHT_STORAGE = ([a-zA-Z]+_\dD)"
    matches = re.search(weight_storage_id, lineStr)
    return matches.group(1)

def isBiasStorageTypeLine(lineStr):
    weight_storage_id = r"^ \* BIAS_STORAGE = "
    return re.search(weight_storage_id, lineStr)

def getBiasStorageType(lineStr):
    weight_storage_id = r"^ \* BIAS_STORAGE = ([a-zA-Z]+_\dD)"
    matches = re.search(weight_storage_id, lineStr)
    return matches.group(1)

typeIdMapping = {
    r"image[123]D\b": "VK_DESCRIPTOR_TYPE_STORAGE_IMAGE",
    r"sampler[123]D\b": "VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER",
    r"\bbuffer\b": "VK_DESCRIPTOR_TYPE_STORAGE_BUFFER",
    r"\buniform\b": "VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER",
}

storageTypeToEnum = {
    "TEXTURE_2D" : "api::StorageType::TEXTURE_2D",
    "TEXTURE_3D" : "api::StorageType::TEXTURE_3D",
    "BUFFER" : "api::StorageType::BUFFER",
    "": "api::StorageType::UNKNOWN",
}

def determineDescriptorType(lineStr):
    for identifier, typeNum in typeIdMapping.items():
        if re.search(identifier, lineStr):
            return typeNum

def getShaderInfo(srcFilePath):
    shader_info = ShaderInfo([], [], "")
    with open(srcFilePath, 'r') as srcFile:
        for line in srcFile:
            if isDescriptorLine(line):
                shader_info.layouts.append(determineDescriptorType(line))
            if isTileSizeLine(line):
                shader_info.tile_size = findTileSizes(line)
            if isWeightStorageTypeLine(line):
                shader_info.weight_storage_type = getWeightStorageType(line)
            if isBiasStorageTypeLine(line):
                shader_info.bias_storage_type = getBiasStorageType(line)

    return shader_info

def genGLSLFromGLSLT(src_dir_path, tmp_dir_path):
    template_dir_path = os.path.join(src_dir_path, "templates")
    vexs = glob.glob(os.path.join(template_dir_path, '**', '*.yaml'), recursive=True)
    parameter_yaml_files = []
    for f in vexs:
        if len(f) > 1:
            parameter_yaml_files.append(f)
    generator = VulkanShaderGenerator()
    for params_yaml in parameter_yaml_files:
        generator.add_params_yaml(params_yaml)  # type: ignore[no-untyped-call]

    vexs = glob.glob(os.path.join(src_dir_path, '**', '*.glslt'), recursive=True)
    templateSrcPaths = []
    for f in vexs:
        if len(f) > 1:
            templateSrcPaths.append(f)
            templateSrcPaths.sort()
    for glslt in templateSrcPaths:
        generator.generate(glslt, tmp_dir_path)  # type: ignore[no-untyped-call]

def genCppH(hFilePath, cppFilePath, srcDirPaths, glslcPath, tmpDirPath, env):
    print("hFilePath:{} cppFilePath:{} srcDirPaths:{} glslcPath:{} tmpDirPath:{}".format(
        hFilePath, cppFilePath, srcDirPaths, glslcPath, tmpDirPath))

    templateSrcPaths = []

    for srcDirPath in srcDirPaths:
        vexs = glob.glob(os.path.join(srcDirPath, '**', '*.glsl'), recursive=True)
        for f in vexs:
            if len(f) > 1:
                templateSrcPaths.append(f)
                templateSrcPaths.sort()

        # Now add glsl files that are generated from templates
        genGLSLFromGLSLT(srcDirPath, tmpDirPath)

    vexs = glob.glob(os.path.join(tmpDirPath, '**', '*.glsl'), recursive=True)
    for f in vexs:
        if len(f) > 1:
            templateSrcPaths.append(f)
            templateSrcPaths.sort()
    print("templateSrcPaths:{}".format(templateSrcPaths))

    spvPaths = {}
    for templateSrcPath in templateSrcPaths:
        print("templateSrcPath {}".format(templateSrcPath))
        name = getName(templateSrcPath).replace("_glsl", "")
        print("name {}".format(name))

        codeTemplate = CodeTemplate.from_file(templateSrcPath)
        srcPath = tmpDirPath + "/" + name + ".glsl"
        content = codeTemplate.substitute(env)
        with open(srcPath, 'w') as f:
            f.write(content)

        spvPath = tmpDirPath + "/" + name + ".spv"
        print("spvPath {}".format(spvPath))

        cmd = [
            glslcPath, "-fshader-stage=compute",
            srcPath, "-o", spvPath,
            "--target-env=vulkan1.0",
            "-Werror"
        ] + [arg for srcDirPath in srcDirPaths for arg in ["-I", srcDirPath]]

        print("\nglslc cmd:", cmd)

        subprocess.check_call(cmd)
        spvPaths[spvPath] = templateSrcPath

    h = "#pragma once\n"
    h += "#include <stdint.h>\n"
    h += "#include <vector>\n"
    h += "#include <string>\n"
    h += "#include <ATen/native/vulkan/api/Types.h>\n"
    h += "#include <ATen/native/vulkan/api/vk_api.h>\n"

    nsbegin = "namespace at {\nnamespace native {\nnamespace vulkan {\n"
    nsend = "} // namespace vulkan\n} // namespace native\n} // namespace at\n"

    h += nsbegin

    # Forward declaration of ShaderInfo
    h += "namespace api {\nstruct ShaderInfo;\n} // namespace api\n"

    cpp = "#include <ATen/native/vulkan/{}>\n".format(H_NAME)
    cpp += "#include <ATen/native/vulkan/api/Shader.h>\n"
    cpp += nsbegin

    shader_info_bin_code = []
    shader_info_cpp_code = []
    shader_info_h_code = []

    for spvPath, srcPath in spvPaths.items():
        name = getName(spvPath)

        print("spvPath:{}".format(spvPath))
        with open(spvPath, 'rb') as f:
            next_bin = array.array('I', f.read())
            sizeBytes = 4 * len(next_bin)
            shader_info_bin_code.append(
                "const uint32_t {}_bin[] = {{\n  {}\n}};".format(
                    name,
                    ",\n  ".join(str(x) for x in next_bin),
                )
            )

        shader_info = getShaderInfo(srcPath)

        tile_size = (
            "{{{}}}".format(", ".join(str(x) for x in shader_info.tile_size))
            if (len(shader_info.tile_size) > 0)
            else "std::vector<uint32_t>()"
        )

        shader_info_args = [
            "\"{}\"".format(name),
            "{}_bin".format(name),
            str(sizeBytes),
            "{{{}}}".format(", ".join(shader_info.layouts)),
            tile_size,
            storageTypeToEnum[shader_info.weight_storage_type],
            storageTypeToEnum[shader_info.bias_storage_type],
        ]


        shader_info_h_code.append("extern const api::ShaderInfo {};".format(name))
        shader_info_cpp_code.append(
            "const api::ShaderInfo {}(\n  {}\n);".format(
                name,
                ",\n  ".join(shader_info_args),
            ),
        )

    cpp += "namespace {{\n{}\n}} // namespace\n".format("\n".join(shader_info_bin_code))
    cpp += "{}\n".format("\n".join(shader_info_cpp_code))
    h += "{}\n".format("\n".join(shader_info_h_code))


    cpp += nsend
    h += nsend

    with open(hFilePath, "w") as f:
        f.write(h)
    with open(cppFilePath, "w") as f:
        f.write(cpp)


def parse_arg_env(items):
    d = {}
    if items:
        for item in items:
            tokens = item.split("=")
            key = tokens[0].strip()
            value = tokens[1].strip()
            d[key] = value
    return d


def main(argv):
    parser = argparse.ArgumentParser(description='')
    parser.add_argument(
        '-i',
        '--glsl-paths',
        nargs='+',
        help='List of paths to look for GLSL source files, separated by spaces. Ex: --glsl-paths "path1 path2 path3"',
        default=['.'],
    )
    parser.add_argument(
        '-c',
        '--glslc-path',
        required=True,
        help='')
    parser.add_argument(
        '-t',
        '--tmp-dir-path',
        required=True,
        help='/tmp')
    parser.add_argument(
        '-o',
        '--output-path',
        required=True,
        help='')
    parser.add_argument(
        "--env",
        metavar="KEY=VALUE",
        nargs='*',
        help="Set a number of key-value pairs")
    options = parser.parse_args()
    env = DEFAULT_ENV
    for key, value in parse_arg_env(options.env).items():
        env[key] = value

    if not os.path.exists(options.output_path):
        os.makedirs(options.output_path)

    if not os.path.exists(options.tmp_dir_path):
        os.makedirs(options.tmp_dir_path)

    genCppH(
        hFilePath=options.output_path + "/spv.h",
        cppFilePath=options.output_path + "/spv.cpp",
        srcDirPaths=options.glsl_paths,
        glslcPath=options.glslc_path,
        tmpDirPath=options.tmp_dir_path,
        env=env)

if __name__ == '__main__':
    sys.exit(main(sys.argv))
