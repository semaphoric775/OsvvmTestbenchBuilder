architecture TbTestTemplate of TestCtrl is

    -- calculate test timeout with crude heuristics
    constant C_TEST_TIMEOUT : time := {{ test_time }};
    -- always include signals for TestDone and TestPhaseStart
    signal TestDone, TestPhaseStart : integer_barrier := 1 ;

{% if test_declarations %}
{{ test_declarations }}

{% endif %}
begin

  ------------------------------------------------------------
  -- ControlProc
  --   Set up AlertLog and wait for end of test
  ------------------------------------------------------------
  ControlProc : process
  begin
    -- Initialization of test
    SetTestName("TbTestTemplate") ;
    SetLogEnable(PASSED, TRUE) ;    -- Enable PASSED logs
    SetLogEnable(INFO, TRUE) ;    -- Enable INFO logs
    SetAlertStopCount(FAILURE, integer'right) ;  -- Allow FAILURES

    -- Wait for testbench initialization
    wait for 0 ns ;  wait for 0 ns ;
    TranscriptOpen ;
    SetTranscriptMirror(TRUE) ;

    -- Wait for Design Reset
    wait until {{ reset_signal }} = {{ reset_active_level }} ;
    ClearAlerts ;

    -- Wait for test to finish
    WaitForBarrier(TestDone, C_TEST_TIMEOUT) ;

    TranscriptClose ;
    -- AffirmIfTranscriptsMatch(PATH_TO_VALIDATED_RESULTS) ;  -- optional transcript check

    EndOfTestReports(TimeOut => (now >= C_TEST_TIMEOUT)) ;

    std.env.stop ;
    wait ;
  end process ControlProc ;

{{ test_processes }}

end architecture TbTestTemplate ;

Configuration TbTestTemplate of {{ tb_toplevel }} is
  for TestHarness
    for TestCtrl_1 : TestCtrl
      use entity work.TestCtrl(TbTestTemplate) ; 
    end for ; 
  end for ; 
end TbTestTemplate ; 
