# -*- coding: utf-8 -*-
"""Maya 2023 standalone 最小探针：确认 mayapy 能不能起来。"""
import os
import sys

print("python:", sys.version.split()[0], flush=True)
print("exe   :", sys.executable, flush=True)

print("step1 import maya.standalone ...", flush=True)
import maya.standalone as sa  # noqa: E402

print("step2 import maya.cmds ...", flush=True)
import maya.cmds as cmds  # noqa: E402

print("step3 initialize ...", flush=True)
sa.initialize()

print("step4 about:", cmds.about(v=True), flush=True)
print("step4b file q scene:", cmds.file(q=True, sn=True), flush=True)

print("step5 loadPlugin fbxmaya ...", flush=True)
if not cmds.pluginInfo("fbxmaya", q=True, loaded=True):
    cmds.loadPlugin("fbxmaya")
print("fbxmaya loaded:", cmds.pluginInfo("fbxmaya", q=True, loaded=True), flush=True)

print("step6 polyCube ...", flush=True)
cmds.polyCube(w=1, h=1, d=1, name="probeCube")
print("objects:", cmds.ls(assemblies=True), flush=True)

print("ALL OK", flush=True)
