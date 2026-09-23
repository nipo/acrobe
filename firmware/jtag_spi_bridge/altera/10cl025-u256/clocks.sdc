# TCK, at the 12 MHz acrobe drives this part at.
create_clock -name tck -period 83.333 [get_ports altera_reserved_tck]

# Internal oscillator.  It runs at about 65 MHz on a CYC1000, yet
# Quartus models the atom as unable to run above about 46 MHz and fails
# pulse width checks at the real rate.  It is constrained at 40 MHz;
# the fabric paths it clocks close there with over 16 ns of slack,
# which covers the real period.
create_clock -name osc -period 25 [get_pins -compatibility_mode {*internal_clock_gen|gen|clkout}]

set_clock_groups -asynchronous -group {tck} -group {osc}

derive_clock_uncertainty

set_false_path -from [get_ports spi_*]
set_false_path -to [get_ports spi_*]
