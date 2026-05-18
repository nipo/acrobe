import asyncio


class JtagFifo:
    """9-bit FIFO transport over JTAG USER DR.
    Matches RTL nsl_bnoc_pipe.

    DR format: [READY (bit 10), VALID (bit 9), data (bits 8:0)]
    Data includes LAST marker in bit 8 (part of the 9-bit word).
    """

    WIDTH = 9
    DR_LEN = WIDTH + 2   # 11 bits: 9 data + VALID + READY
    VALID = 1 << WIDTH            # bit 9
    READY = 1 << (WIDTH + 1)      # bit 10
    DMASK = (1 << WIDTH) - 1      # bits 8:0
    LAST = 0x100                  # bit 8 of the 9-bit word

    def __init__(self, tap, data_ir, status_ir=None):
        self.__tap = tap
        self.__data = tap.ir(data_ir, dr_length=self.DR_LEN)
        self.__status = tap.ir(status_ir, dr_length=32) if status_ir is not None else None

    async def exchange(self, tx_words, expect_frames=1):
        """Send tx_words and receive until expect_frames complete frames seen.

        Each frame ends with the LAST bit (bit 8) set in a data word.
        Returns flat list of received 9-bit words (including LAST markers).
        """
        if self.__status is not None:
            return await self.__exchange_speculative(tx_words, expect_frames)
        return await self.__exchange_single(tx_words, expect_frames)

    async def __exchange_single(self, tx_words, expect_frames):
        """One DR shift per round. Checks VALID/READY from TDO."""
        tx_idx = 0
        rx_words = []
        frames_seen = 0
        idle_count = 0

        while frames_seen < expect_frames or tx_idx < len(tx_words):
            # Build TDI word
            if tx_idx < len(tx_words):
                tdi = self.VALID | self.READY | tx_words[tx_idx]
            else:
                tdi = self.READY

            result = await self.__data(tdi)
            tdo = int(result)

            ready = bool(tdo & self.READY)
            valid = bool(tdo & self.VALID)
            data = tdo & self.DMASK

            if tx_idx < len(tx_words) and ready:
                tx_idx += 1

            if valid:
                rx_words.append(data)
                idle_count = 0
                if data & JtagFifo.LAST:
                    frames_seen += 1
            else:
                idle_count += 1
                if idle_count > 1000:
                    raise RuntimeError("FIFO exchange timeout: no data received")

        return rx_words

    async def __exchange_speculative(self, tx_words, expect_frames):
        """Batch multiple DR shifts using status register for queue depths."""
        tx_idx = 0
        rx_words = []
        frames_seen = 0

        while frames_seen < expect_frames or tx_idx < len(tx_words):
            # Read status: out_free (bits 31:16), in_ready (bits 15:0)
            status_result = await self.__status()
            status = int(status_result)
            out_free = (status >> 16) & 0xffff
            in_ready = (status & 0xffff) + 16  # margin

            # Build batch of DR shifts
            shifts = []
            count = 0
            while tx_idx < len(tx_words) and count < out_free:
                tdi = self.VALID | self.READY | tx_words[tx_idx]
                shifts.append(self.__data(tdi))
                # Interleave run cycles for firmware processing
                self.__tap.run(10)
                tx_idx += 1
                count += 1

            # Pad with dummy reads
            while count < min(in_ready, 128):
                shifts.append(self.__data(self.READY))
                self.__tap.run(10)
                count += 1

            if not shifts:
                # Nothing to do, run some cycles and retry
                await self.__tap.run(100)
                continue

            results = await asyncio.gather(*shifts)

            for result in results:
                tdo = int(result)
                valid = bool(tdo & self.VALID)
                data = tdo & self.DMASK

                if valid:
                    rx_words.append(data)
                    if data & JtagFifo.LAST:
                        frames_seen += 1

        return rx_words
