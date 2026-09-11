"""Run commands and transfer files after a workload target has been selected.

Connection target caches, SSH/Jupyter selection, execution capture, staged file
publication and async output sinks belong here. Transport choice must preserve
the caller's target and path semantics. Interactive terminal UI belongs to
inspire.cli; workload creation and stop/wait policy belong to the workload domain.
"""
