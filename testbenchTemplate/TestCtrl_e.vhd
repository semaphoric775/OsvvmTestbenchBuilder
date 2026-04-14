library ieee ;
  use ieee.std_logic_1164.all ;
  use ieee.numeric_std.all ;
  use ieee.numeric_std_unsigned.all ;

library OSVVM ;
  context OSVVM.OsvvmContext ;


{% if project_libraries %}
{{ project_libraries }}

{% endif %}
{% if dut_libraries %}
{{ dut_libraries }}

{% endif %}
entity TestCtrl is
{{ generic_section }}
    port (
        -- Always propagate reset and clock signals to all testbench components
        {{ clock_signal }}       : In    std_logic ;
        {{ resetn_signal }}      : In    std_logic {{ semicolon_needed}}
{% if ports_section %}
{{ ports_section }}
{% endif %}
    ) ;
    {{ generic_calculations }}

    {{ fifo_aliases }}
end TestCtrl ;
