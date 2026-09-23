set_global_assignment -name STRATIX_DEVICE_IO_STANDARD "3.3-V LVTTL"
set_global_assignment -name RESERVE_ALL_UNUSED_PINS "AS INPUT TRI-STATED"

# Active serial configuration pins, handed to the fabric once the part
# is configured.
set_global_assignment -name RESERVE_DCLK_AFTER_CONFIGURATION "USE AS REGULAR IO"
set_global_assignment -name RESERVE_DATA0_AFTER_CONFIGURATION "USE AS REGULAR IO"
set_global_assignment -name RESERVE_DATA1_AFTER_CONFIGURATION "USE AS REGULAR IO"
set_global_assignment -name RESERVE_FLASH_NCE_AFTER_CONFIGURATION "USE AS REGULAR IO"

# The altera_reserved_* JTAG ports need no assignment: Quartus puts
# them on the dedicated JTAG pads.

set_location_assignment PIN_H1 -to spi_sck_o
set_location_assignment PIN_H2 -to spi_miso_i
set_location_assignment PIN_C1 -to spi_mosi_io
set_location_assignment PIN_D2 -to spi_cs_n_io
set_instance_assignment -name WEAK_PULL_UP_RESISTOR ON -to spi_cs_n_io
