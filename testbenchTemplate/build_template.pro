library {{ libraryname }}

{{ toolsettings }}

{{ analyze_project_section }}

# Analyze testbench files
analyze TestCtrl_e.vhd
analyze {{ testbench_top }}
analyze {{ tbtest_file }}

# Set debug mode
SetDebugMode {{ debug_mode }}
SetLogSignals {{ log_mode }}

# Run test
simulate {{ base_test }}
