"""Curated OSVVM transaction API extracted from the actual library source.

All procedure signatures below are pulled verbatim from:
  - OsvvmLibraries/Common/src/AddressBusTransactionPkg.vhd
  - OsvvmLibraries/Common/src/StreamTransactionPkg.vhd
  - OsvvmLibraries/UART/src/UartTbPkg.vhd (for UART error constants)

This module is imported by the transaction generation node and cached at module
load time — no file I/O at generation time.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# AddressBus (AXI4, AXI4Lite) — Manager side
# Source: AddressBusTransactionPkg.vhd
# ---------------------------------------------------------------------------
ADDRESS_BUS_MANAGER_API = """\
-- ── Directive transactions (no bus activity) ──────────────────────────────
procedure WaitForClock(signal TransactionRec : inout Axi4LiteRecType; constant NumberOfClocks : natural := 1);
procedure WaitForTransaction(signal TransactionRec : inout Axi4LiteRecType);
procedure WaitForWriteTransaction(signal TransactionRec : inout Axi4LiteRecType);
procedure WaitForReadTransaction(signal TransactionRec : inout Axi4LiteRecType);
procedure GetTransactionCount(signal TransactionRec : inout Axi4LiteRecType; variable Count : out integer);
procedure GetErrorCount(signal TransactionRec : inout Axi4LiteRecType; variable ErrorCount : out natural);

-- ── Blocking interface transactions ───────────────────────────────────────
procedure Write(
    signal   TransactionRec : inout Axi4LiteRecType;
             iAddr          : in    std_logic_vector;   -- e.g. X"0000_0000"
             iData          : in    std_logic_vector;   -- e.g. X"DEAD_BEEF"
             StatusMsgOn    : in    boolean := false);

procedure Read(
    signal   TransactionRec : inout Axi4LiteRecType;
             iAddr          : in    std_logic_vector;
    variable oData          : out   std_logic_vector;
             StatusMsgOn    : in    boolean := false);

procedure ReadCheck(
    signal   TransactionRec : inout Axi4LiteRecType;
             iAddr          : in    std_logic_vector;
             iData          : in    std_logic_vector;   -- expected value
             StatusMsgOn    : in    boolean := false);

procedure ReadPoll(
    signal   TransactionRec : inout Axi4LiteRecType;
             iAddr          : in    std_logic_vector;
    variable oData          : out   std_logic_vector;
             Index          : in    integer;             -- bit index to poll
             BitValue       : in    std_logic;           -- '0' or '1'
             StatusMsgOn    : in    boolean := false;
             WaitTime       : in    natural := 10);      -- clocks between reads

-- ── Asynchronous (non-blocking) variants ──────────────────────────────────
procedure WriteAsync(signal TransactionRec : inout Axi4LiteRecType; iAddr : in std_logic_vector; iData : in std_logic_vector);
procedure ReadAddressAsync(signal TransactionRec : inout Axi4LiteRecType; iAddr : in std_logic_vector);
procedure ReadData(signal TransactionRec : inout Axi4LiteRecType; variable oData : out std_logic_vector);
procedure ReadCheckData(signal TransactionRec : inout Axi4LiteRecType; iData : in std_logic_vector);

-- ── Burst transactions ────────────────────────────────────────────────────
procedure WriteBurstVector(
    signal   TransactionRec : inout Axi4LiteRecType;
             iAddr          : in    std_logic_vector;
             VectorOfWords  : in    slv_vector;          -- array of std_logic_vector
             StatusMsgOn    : in    boolean := false);

procedure ReadCheckBurstVector(
    signal   TransactionRec : inout Axi4LiteRecType;
             iAddr          : in    std_logic_vector;
             VectorOfWords  : in    slv_vector;
             StatusMsgOn    : in    boolean := false);

-- ── Scoreboard (idiomatic OSVVM) ─────────────────────────────────────────
-- Architecture declaration (use fully-qualified name — no extra use clause needed):
--   shared variable <Rec>_SB : osvvm.ScoreboardPkg_slv.ScoreboardPType ;
--
-- Pattern for write-then-read checking:
--   <Rec>_SB.Push(X"DEAD_BEEF") ;              -- record expected value
--   Write(TransactionRec, X"0000_0010", X"DEAD_BEEF") ;
--   <Rec>_SB.Check(ReadData) ;                 -- pops expected, checks received
-- Or use ReadCheck directly when expected value is known at write time:
--   ReadCheck(TransactionRec, X"0000_0010", X"DEAD_BEEF") ;
"""

# ---------------------------------------------------------------------------
# AddressBus (AXI4, AXI4Lite) — Subordinate side
# Source: AddressBusTransactionPkg.vhd  (Responder / VC side)
# The subordinate VC drives write response and read data back to the manager.
# In a testbench this side is usually implemented by an OSVVM Memory model,
# but when testing a subordinate DUT, the test controller must act as manager.
# ---------------------------------------------------------------------------
ADDRESS_BUS_SUBORDINATE_NOTES = """\
-- When DUT is an AXI4Lite Subordinate, the test controller uses the Manager
-- VC (Axi4LiteManager) to drive transactions TO the DUT.  The same Write/Read
-- procedures above apply — the role distinction is in the DUT, not the test.
--
-- Useful assertion helper (from AlertLogPkg, always available):
procedure AffirmIfEqual(AlertLogID : AlertLogIDType; Received : std_logic_vector; Expected : std_logic_vector; Message : string := "");
"""

# ---------------------------------------------------------------------------
# Stream (AXI4Stream) — Transmitter side
# Source: StreamTransactionPkg.vhd
# ---------------------------------------------------------------------------
STREAM_TRANSMITTER_API = """\
-- ── Directive transactions ────────────────────────────────────────────────
procedure WaitForClock(signal TransactionRec : inout StreamRecType; constant NumberOfClocks : natural := 1);
procedure WaitForTransaction(signal TransactionRec : inout StreamRecType);
procedure GetTransactionCount(signal TransactionRec : inout StreamRecType; variable Count : out integer);
procedure GetErrorCount(signal TransactionRec : inout StreamRecType; variable ErrorCount : out natural);

-- ── Blocking send ─────────────────────────────────────────────────────────
procedure Send(
    signal   TransactionRec : inout StreamRecType;
    constant Data           : in    std_logic_vector;   -- tdata word
    constant StatusMsgOn    : in    boolean := false);

-- ── Asynchronous send ─────────────────────────────────────────────────────
procedure SendAsync(signal TransactionRec : inout StreamRecType; constant Data : in std_logic_vector);

-- ── Burst sends ───────────────────────────────────────────────────────────
procedure SendBurstVector(
    signal   TransactionRec : inout StreamRecType;
    constant VectorOfWords  : in    slv_vector;
    constant StatusMsgOn    : in    boolean := false);

procedure SendBurstIncrement(
    signal   TransactionRec : inout StreamRecType;
    constant FirstWord      : in    std_logic_vector;
    constant NumFifoWords   : in    integer;
    constant StatusMsgOn    : in    boolean := false);

procedure SendBurstRandom(
    signal   TransactionRec : inout StreamRecType;
    constant FirstWord      : in    std_logic_vector;
    constant NumFifoWords   : in    integer;
    constant StatusMsgOn    : in    boolean := false);

-- ── Checking DUT outputs after Send ──────────────────────────────────────
-- After Send completes, call WaitForTransaction then AffirmIfEqual on plain
-- DUT output signals to verify behaviour.  This is the correct pattern for a
-- transmitter driving a sink DUT:
--
--   Send(TransactionRec, X"AB") ;
--   WaitForTransaction(TransactionRec) ;
--   AffirmIfEqual(dut_output, expected_slv, "description") ;
--
-- AffirmIfEqual (from AlertLogPkg, always in scope via OsvvmContext):
procedure AffirmIfEqual(Received : std_logic_vector; Expected : std_logic_vector; Message : string := "");
--
-- Do NOT use a scoreboard in the transmitter role — there is no paired receiver
-- to Pop from it, so Push calls are wasted and Check calls will hang.
"""

# ---------------------------------------------------------------------------
# Stream (AXI4Stream) — Receiver side
# Source: StreamTransactionPkg.vhd
# ---------------------------------------------------------------------------
STREAM_RECEIVER_API = """\
-- ── Blocking get / check ──────────────────────────────────────────────────
procedure Get(
    signal   TransactionRec : inout StreamRecType;
    variable Data           : out   std_logic_vector;
    constant StatusMsgOn    : in    boolean := false);

procedure Check(
    signal   TransactionRec : inout StreamRecType;
    constant Data           : in    std_logic_vector;   -- expected value
    constant StatusMsgOn    : in    boolean := false);

-- ── Non-blocking try variants ─────────────────────────────────────────────
procedure TryGet(
    signal   TransactionRec : inout StreamRecType;
    variable Data           : out   std_logic_vector;
    variable Available      : out   boolean;
    constant StatusMsgOn    : in    boolean := false);

procedure TryCheck(
    signal   TransactionRec : inout StreamRecType;
    constant Data           : in    std_logic_vector;
    variable Available      : out   boolean;
    constant StatusMsgOn    : in    boolean := false);

-- ── Burst get / check ─────────────────────────────────────────────────────
procedure GetBurst(signal TransactionRec : inout StreamRecType; variable NumFifoWords : inout integer);

procedure CheckBurstVector(
    signal   TransactionRec : inout StreamRecType;
    constant VectorOfWords  : in    slv_vector;
    constant StatusMsgOn    : in    boolean := false);

-- ── Scoreboard ────────────────────────────────────────────────────────────
-- Architecture declaration:
--   shared variable <Rec>_SB : osvvm.ScoreboardPkg_slv.ScoreboardPType ;
--
-- Pop expected from paired transmitter scoreboard and check received:
--   Get(TransactionRec, ReceivedData) ;
--   <Rec>_SB.Check(ReceivedData) ;   -- pops expected, checks received
"""

# ---------------------------------------------------------------------------
# UART — Transmitter (UartTx) side
# Source: StreamTransactionPkg.vhd + UartTbPkg.vhd
# UART uses the Stream interface with Param for error injection.
# ---------------------------------------------------------------------------
UART_TX_API = """\
-- UART error constants (from UartTbPkg.vhd):
--   UARTTB_NO_ERROR     : std_logic_vector(3 downto 1) := "000"
--   UARTTB_PARITY_ERROR : bit 1 set
--   UARTTB_STOP_ERROR   : bit 2 set
--   UARTTB_BREAK_ERROR  : bit 3 set

procedure WaitForClock(signal TransactionRec : inout UartRecType; constant NumberOfClocks : natural := 1);
procedure GetTransactionCount(signal TransactionRec : inout UartRecType; variable Count : out integer);

-- Blocking send (no error injection):
procedure Send(signal TransactionRec : inout UartRecType; constant Data : in std_logic_vector);

-- Blocking send with error injection via Param:
procedure Send(
    signal   TransactionRec : inout UartRecType;
    constant Data           : in    std_logic_vector;   -- 8-bit byte e.g. X"A5"
    constant Param          : in    std_logic_vector);  -- error flags e.g. UARTTB_PARITY_ERROR

-- ── Scoreboard ────────────────────────────────────────────────────────────
-- Architecture declaration:
--   shared variable <Rec>_SB : osvvm.ScoreboardPkg_Uart.ScoreboardPType ;
--
-- Push before send so receiver can check:
--   <Rec>_SB.Push(X"A5") ;
--   Send(TransactionRec, X"A5") ;
"""

# ---------------------------------------------------------------------------
# UART — Receiver (UartRx) side
# Source: StreamTransactionPkg.vhd + UartTbPkg.vhd
# ---------------------------------------------------------------------------
UART_RX_API = """\
procedure WaitForClock(signal TransactionRec : inout UartRecType; constant NumberOfClocks : natural := 1);

-- Blocking get (data only):
procedure Get(signal TransactionRec : inout UartRecType; variable Data : out std_logic_vector);

-- Blocking get with error status:
procedure Get(
    signal   TransactionRec : inout UartRecType;
    variable Data           : out   std_logic_vector;
    variable Param          : out   std_logic_vector);  -- received error flags

-- Blocking check (data only):
procedure Check(signal TransactionRec : inout UartRecType; constant Data : in std_logic_vector);

-- Check with expected error status:
procedure Check(
    signal   TransactionRec : inout UartRecType;
    constant Data           : in    std_logic_vector;
    constant Param          : in    std_logic_vector);  -- expected error flags

-- Non-blocking try:
procedure TryGet(
    signal   TransactionRec : inout UartRecType;
    variable Data           : out   std_logic_vector;
    variable Available      : out   boolean);

-- ── Scoreboard ────────────────────────────────────────────────────────────
-- Architecture declaration:
--   shared variable <Rec>_SB : osvvm.ScoreboardPkg_Uart.ScoreboardPType ;
--
-- Get received byte and check against scoreboard:
--   Get(TransactionRec, ReceivedData) ;
--   <Rec>_SB.Check(ReceivedData) ;
"""

# ---------------------------------------------------------------------------
# Lookup: map (vc_type, role) → API string / scoreboard type
# ---------------------------------------------------------------------------
def get_api(vc_type: str, role: str) -> str:
    key = (vc_type.lower(), role.lower())
    return _API_MAP.get(key, "-- No curated API available for this VC type/role.\n")


def get_scoreboard_pkg(vc_type: str) -> str:
    """Return the fully-qualified OSVVM scoreboard package for this VC type."""
    if vc_type.lower() in ("uart",):
        return "osvvm.ScoreboardPkg_Uart"
    return "osvvm.ScoreboardPkg_slv"


_API_MAP: dict[tuple[str, str], str] = {
    ("axi4lite", "manager"):     ADDRESS_BUS_MANAGER_API,
    ("axi4lite", "subordinate"): ADDRESS_BUS_MANAGER_API + "\n" + ADDRESS_BUS_SUBORDINATE_NOTES,
    ("axi4",     "manager"):     ADDRESS_BUS_MANAGER_API,
    ("axi4",     "subordinate"): ADDRESS_BUS_MANAGER_API + "\n" + ADDRESS_BUS_SUBORDINATE_NOTES,
    ("axi4stream", "manager"):   STREAM_TRANSMITTER_API,
    ("axi4stream", "subordinate"): STREAM_RECEIVER_API,
    ("uart", "manager"):         UART_TX_API,
    ("uart", "subordinate"):     UART_RX_API,
}
