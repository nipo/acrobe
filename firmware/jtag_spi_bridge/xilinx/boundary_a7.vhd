library ieee;
use ieee.std_logic_1164.all;

library work, nsl_io, jtag_spi;

entity boundary is
  port (
    spi_cs_n_o: inout std_logic;
    spi_mosi_o: inout std_ulogic;
    spi_miso_i: in std_ulogic
  );
end boundary;

architecture arch of boundary is

  attribute BOX_TYPE : string;

  component STARTUPE2
    generic (
      PROG_USR : string := "FALSE";
      SIM_CCLK_FREQ : real := 0.0
      );
    port (
      CFGCLK : out std_ulogic;
      CFGMCLK : out std_ulogic;
      EOS : out std_ulogic;
      PREQ : out std_ulogic;
      CLK : in std_ulogic;
      GSR : in std_ulogic;
      GTS : in std_ulogic;
      KEYCLEARB : in std_ulogic;
      PACK : in std_ulogic;
      USRCCLKO : in std_ulogic;
      USRCCLKTS : in std_ulogic;
      USRDONEO : in std_ulogic;
      USRDONETS : in std_ulogic
      );
  end component;
  attribute BOX_TYPE of
    STARTUPE2 : component is "PRIMITIVE";

  signal reset_n_s, clock_s : std_ulogic;
  signal spi_sck_s: std_ulogic;
  signal spi_cs_n_s: nsl_io.io.opendrain;
  signal done_led_n_s, led_s : std_ulogic;
  signal spi_mosi_s: nsl_io.io.tristated;
  
begin

  startupe2_inst : startupe2
    port map (
      cfgmclk => clock_s,
      eos => reset_n_s,
      clk => '0',
      gsr => '0',
      gts => '0',
      keyclearb => '1',
      pack => '0',
      usrcclko => spi_sck_s,
      usrcclkts => '0', -- oe_n
      usrdoneo => '0',
      usrdonets => done_led_n_s -- oe_n
      );

  done_led_n_s <= not led_s;
  
  main: jtag_spi.bridge.jtag_spi_bridge
    generic map(
      clock_hz_c => 55e6
      )
    port map(
      reset_n_i => reset_n_s,
      clock_i => clock_s,
      spi_o.sck => spi_sck_s,
      spi_o.mosi => spi_mosi_s,
      spi_o.cs_n => spi_cs_n_s,
      spi_i.miso => spi_miso_i,
      led_o => led_s
      );
  
  cs_driver: nsl_io.io.opendrain_io_driver
    port map(
      io_io => spi_cs_n_o,
      v_i => spi_cs_n_s
      );

  mosi_driver: nsl_io.io.tristated_io_driver
    port map(
      io_io => spi_mosi_o,
      v_i => spi_mosi_s
      );

end arch;
