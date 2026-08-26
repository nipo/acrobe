library ieee;
use ieee.std_logic_1164.all;

library work, nsl_io, nsl_hwdep, jtag_spi;

entity boundary is
  port (
    spi_cs_n_io: inout std_logic;
    spi_mosi_io: inout std_logic;
    spi_miso_io: inout std_logic
  );
end boundary;

architecture arch of boundary is

  signal reset_n_s, clock_s, spi_sck_s, spi_sck_ts_s : std_ulogic;
  signal spi_cs_n_s: nsl_io.io.opendrain;
  signal spi_mosi_s: nsl_io.io.tristated;

  COMPONENT USRMCLK
    PORT(
      USRMCLKI : IN STD_ULOGIC;
      USRMCLKTS : IN STD_ULOGIC
      );
  END COMPONENT;
  attribute syn_noprune: boolean ;
  attribute syn_noprune of USRMCLK: component is true;  

begin

  sck: USRMCLK
    port map(
      USRMCLKI => spi_sck_s,
      USRMCLKTS => "not"(spi_mosi_s.en)
      );
  spi_sck_ts_s <= '0';
  
  internal_clock_gen: nsl_hwdep.clock.clock_internal
    port map(
      clock_o => clock_s
      );

  internal_reset_gen: nsl_hwdep.reset.reset_at_startup
    port map(
      clock_i => clock_s,
      reset_n_o => reset_n_s
      );

  mosi: nsl_io.io.tristated_io_driver
    port map(
      io_io => spi_mosi_io,
      v_i => spi_mosi_s,
      v_o => open);
  
  main: jtag_spi.bridge.jtag_spi_bridge
    generic map(
      clock_hz_c => 65e6
      )
    port map(
      reset_n_i => reset_n_s,
      clock_i => clock_s,

      spi_o.sck => spi_sck_s,
      spi_o.mosi => spi_mosi_s,
      spi_o.cs_n => spi_cs_n_s,
      spi_i.miso => spi_miso_io,

      led_o => open
      );
  
  cs_driver: nsl_io.io.opendrain_io_driver
    port map(
      io_io => spi_cs_n_io,
      v_i => spi_cs_n_s
      );

  spi_miso_io <= 'Z';

end arch;
