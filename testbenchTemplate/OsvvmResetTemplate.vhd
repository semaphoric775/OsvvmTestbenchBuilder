  Osvvm.ClockResetPkg.CreateReset ( 
    Reset       => {{ reset }},
    ResetActive => {{ reset_active }},
    Clk         => {{ clock }},
    Period      => 10 * {{ clock_period }},
    tpd         => {{ reset_tpd }}
  ) ;
  