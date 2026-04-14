library {{ libraryname }}

{{ toolsettings }}

# Toplevel path containing all blocks used in project
#   Use path relative to project directory to support multiple users
set BASE_DIR {{ reldir }}
# Directory containing toplevel design
set PROJECT_DIR {{ project_dir }}

{{ analyze_project_section }}

# Analyze testbench files
analyze TestCtrl_e.vhd
analyze {{ testbench_top }}
analyze {{ tbtest_file }}

# Set debug mode
SetDebugMode {{ debug_mode }}
SetLogSignals {{ log_mode }}

# Run test
RunTest {{ base_test }}
