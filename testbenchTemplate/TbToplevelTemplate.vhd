library ieee ;
  use ieee.std_logic_1164.all ;
  use ieee.numeric_std.all ;
  use ieee.numeric_std_unsigned.all ;


{% if project_libraries %}
{{ project_libraries }}

{% endif %}
{% if dut_libraries %}
{{ dut_libraries }}

{% endif %}
entity {{ TbTopLevelTemplate }} is
end entity {{ TbTopLevelTemplate }} ;
architecture TestHarness of {{ TbTopLevelTemplate }} is

{{ constant_clk_per_definitions }}

{{ clock_definitions }}

{{ reset_definitions }}

{{ signal_definitions }}

{{ verification_component_records }}

{{ component_declarations }}

{{ TestCtrl_definition }}

begin
    -- Use OSVVM utilities to generate clock and reset
{{ clock_instantiations }}

{{ reset_instantiations }}

    -- OSVVM Verification Components
{{ osvvm_vc_instantiations }}

    DUT : entity work.{{ DUT_entity_name }}
        port map (
            {{ DUT_port_mappings }}
        ) ;

    TestCtrl_1 : {{ TestCtrl_instantiation }}
        port map (
            {{ TestCtrl_port_mappings }}
        ) ;

end architecture TestHarness ;
