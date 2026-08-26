library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_bnoc, nsl_spi, nsl_jtag, nsl_indication, nsl_io, nsl_clocking;

entity jtag_spi_bridge is
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
end jtag_spi_bridge;

architecture arch of jtag_spi_bridge is

  type framed_io is
  record
    cmd, rsp : nsl_bnoc.framed.framed_bus;
  end record;
  
  type slave_conns is
  record
    post_fifo : framed_io;
    pre_fifo : framed_io;
  end record;

  signal comm_spi : slave_conns;

  signal reset_n_s, tap_reset_n_s : std_ulogic;
  
begin

  reset_sync: nsl_clocking.async.async_edge
    port map(
      clock_i => clock_i,
      data_i => tap_reset_n_s,
      data_o => reset_n_s
      );
  
  act: nsl_indication.activity.activity_blinker
    generic map(
      clock_hz_c => real(clock_hz_c)
      )
    port map(
      reset_n_i => reset_n_s,
      clock_i => clock_i,
      activity_i => comm_spi.pre_fifo.rsp.req.valid,
      led_o => led_o
      );

--  jtag_io: nsl_jtag.fifo_transport.jtag_framed_transport_tap
--    port map(
--      clock_i => clock_i,
--      reset_n_i => reset_n_i,
--      reset_n_o => reset_n_s,
--
--      chip_tdi_i => chip_tdi_i,
--      chip_tck_i => chip_tck_i,
--      chip_tms_i => chip_tms_i,
--      chip_tdo_o => chip_tdo_o,
--
--      tx_i => comm_spi.pre_fifo.rsp.req,
--      tx_o => comm_spi.pre_fifo.rsp.ack,
--
--      rx_o => comm_spi.pre_fifo.cmd.req,
--      rx_i => comm_spi.pre_fifo.cmd.ack
--      );
  
  jtag_io: nsl_jtag.continuous_transport.jtag_continuous_transport_tap
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      reset_n_o => tap_reset_n_s,

      chip_tdi_i => chip_tdi_i,
      chip_tck_i => chip_tck_i,
      chip_tms_i => chip_tms_i,
      chip_tdo_o => chip_tdo_o,

      tx_i => comm_spi.pre_fifo.rsp.req,
      tx_o => comm_spi.pre_fifo.rsp.ack,

      rx_o => comm_spi.pre_fifo.cmd.req,
      rx_i => comm_spi.pre_fifo.cmd.ack
      );

  inbound_fifo: nsl_bnoc.framed.framed_fifo
    generic map(
      depth => 512,
      clk_count => 1
      )
    port map(
      p_resetn => reset_n_s,
      p_clk(0) => clock_i,

      p_in_val => comm_spi.pre_fifo.cmd.req,
      p_in_ack => comm_spi.pre_fifo.cmd.ack,

      p_out_val => comm_spi.post_fifo.cmd.req,
      p_out_ack => comm_spi.post_fifo.cmd.ack
      );

  outbound_fifo: nsl_bnoc.framed.framed_fifo
    generic map(
      depth => 512,
      clk_count => 1
      )
    port map(
      p_resetn => reset_n_s,
      p_clk(0) => clock_i,

      p_out_val => comm_spi.pre_fifo.rsp.req,
      p_out_ack => comm_spi.pre_fifo.rsp.ack,

      p_in_val => comm_spi.post_fifo.rsp.req,
      p_in_ack => comm_spi.post_fifo.rsp.ack
      );

  spi_inst: nsl_spi.transactor.spi_framed_transactor
    generic map(
      slave_count_c => 1
      )
    port map(
      clock_i  => clock_i,
      reset_n_i => reset_n_s,
      
      sck_o => spi_o.sck,
      cs_n_o(0) => spi_o.cs_n,
      mosi_o => spi_o.mosi,
      miso_i => spi_i.miso,

      cmd_i => comm_spi.post_fifo.cmd.req,
      cmd_o => comm_spi.post_fifo.cmd.ack,
      rsp_o => comm_spi.post_fifo.rsp.req,
      rsp_i => comm_spi.post_fifo.rsp.ack
      );

end arch;
