library ieee;
use ieee.std_logic_1164.all;

library nsl_spi;

package bridge is
  component jtag_spi_bridge is
    generic(
      clock_hz_c : integer
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      chip_tdi_i: in std_ulogic := '0';
      chip_tck_i: in std_ulogic := '0';
      chip_tms_i: in std_ulogic := '0';
      chip_tdo_o: out std_logic;

      spi_o: out nsl_spi.spi.spi_master_o;
      spi_i: in nsl_spi.spi.spi_master_i;
      led_o : out std_ulogic
      );
  end component;
end package;
