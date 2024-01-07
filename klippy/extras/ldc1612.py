# Support for reading frequency samples from ldc1612
#
# Copyright (C) 2020-2024  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
from . import bus, bulk_sensor

MIN_MSG_TIME = 0.100

BATCH_UPDATES = 0.100

BYTES_PER_SAMPLE = 4
SAMPLES_PER_BLOCK = bulk_sensor.MAX_BULK_MSG_SIZE // BYTES_PER_SAMPLE

LDC1612_ADDR = 0x2a

LDC1612_FREQ = 12000000
SETTLETIME = 0.005
DRIVECUR = 15
DEGLITCH = 0x05 # 10 Mhz

LDC1612_MANUF_ID = 0x5449
LDC1612_DEV_ID = 0x3055

REG_RCOUNT0 = 0x08
REG_OFFSET0 = 0x0c
REG_SETTLECOUNT0 = 0x10
REG_CLOCK_DIVIDERS0 = 0x14
REG_ERROR_CONFIG = 0x19
REG_CONFIG = 0x1a
REG_MUX_CONFIG = 0x1b
REG_DRIVE_CURRENT0 = 0x1e
REG_MANUFACTURER_ID = 0x7e
REG_DEVICE_ID = 0x7f

# Tool for determining appropriate DRIVE_CURRENT register
class DriveCurrentCalibrate:
    def __init__(self, config, sensor):
        self.printer = config.get_printer()
        self.sensor = sensor
        self.drive_cur = config.getint("reg_drive_current", DRIVECUR,
                                       minval=0, maxval=31)
        self.name = config.get_name()
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command("LDC_CALIBRATE_DRIVE_CURRENT",
                                   "CHIP", self.name.split()[-1],
                                   self.cmd_LDC_CALIBRATE,
                                   desc=self.cmd_LDC_CALIBRATE_help)
    def get_drive_current(self):
        return self.drive_cur
    cmd_LDC_CALIBRATE_help = "Calibrate LDC1612 DRIVE_CURRENT register"
    def cmd_LDC_CALIBRATE(self, gcmd):
        is_in_progress = True
        def handle_batch(msg):
            return is_in_progress
        self.sensor.add_client(handle_batch)
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.dwell(0.100)
        toolhead.wait_moves()
        old_config = self.sensor.read_reg(REG_CONFIG)
        self.sensor.set_reg(REG_CONFIG, 0x001 | (1<<9))
        toolhead.wait_moves()
        toolhead.dwell(0.100)
        toolhead.wait_moves()
        reg_drive_current0 = self.sensor.read_reg(REG_DRIVE_CURRENT0)
        self.sensor.set_reg(REG_CONFIG, old_config)
        is_in_progress = False
        # Report found value to user
        drive_cur = (reg_drive_current0 >> 6) & 0x1f
        gcmd.respond_info(
            "%s: reg_drive_current: %d\n"
            "The SAVE_CONFIG command will update the printer config file\n"
            "with the above and restart the printer." % (self.name, drive_cur))
        configfile = self.printer.lookup_object('configfile')
        configfile.set(self.name, 'reg_drive_current', "%d" % (drive_cur,))

# Interface class to LDC1612 mcu support
class LDC1612:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.dccal = DriveCurrentCalibrate(config, self)
        self.data_rate = 250
        # Setup mcu sensor_ldc1612 bulk query code
        self.i2c = bus.MCU_I2C_from_config(config,
                                           default_addr=LDC1612_ADDR,
                                           default_speed=400000)
        self.mcu = mcu = self.i2c.get_mcu()
        self.oid = oid = mcu.create_oid()
        self.query_ldc1612_cmd = None
        mcu.add_config_cmd("config_ldc1612 oid=%d i2c_oid=%d"
                           % (oid, self.i2c.get_oid()))
        mcu.add_config_cmd("query_ldc1612 oid=%d rest_ticks=0"
                           % (oid,), on_restart=True)
        mcu.register_config_callback(self._build_config)
        self.bulk_queue = bulk_sensor.BulkDataQueue(mcu, oid=oid)
        # Clock tracking
        chip_smooth = self.data_rate * BATCH_UPDATES * 2
        self.clock_sync = bulk_sensor.ClockSyncRegression(mcu, chip_smooth)
        self.clock_updater = bulk_sensor.ChipClockUpdater(self.clock_sync,
                                                          BYTES_PER_SAMPLE)
        self.last_error_count = 0
        # Process messages in batches
        self.batch_bulk = bulk_sensor.BatchBulkHelper(
            self.printer, self._process_batch,
            self._start_measurements, self._finish_measurements, BATCH_UPDATES)
        self.name = config.get_name().split()[-1]
        hdr = ('time', 'frequency')
        self.batch_bulk.add_mux_endpoint("ldc1612/dump_ldc1612", "sensor",
                                         self.name, {'header': hdr})
    def _build_config(self):
        cmdqueue = self.i2c.get_command_queue()
        self.query_ldc1612_cmd = self.mcu.lookup_command(
            "query_ldc1612 oid=%c rest_ticks=%u", cq=cmdqueue)
        self.clock_updater.setup_query_command(
            self.mcu, "query_ldc1612_status oid=%c", oid=self.oid, cq=cmdqueue)
    def read_reg(self, reg):
        params = self.i2c.i2c_read([reg], 2)
        response = bytearray(params['response'])
        return (response[0] << 8) | response[1]
    def set_reg(self, reg, val, minclock=0):
        self.i2c.i2c_write([reg, (val >> 8) & 0xff, val & 0xff],
                           minclock=minclock)
    def add_client(self, cb):
        self.batch_bulk.add_client(cb)
    # Measurement decoding
    def _extract_samples(self, raw_samples):
        # Load variables to optimize inner loop below
        last_sequence = self.clock_updater.get_last_sequence()
        time_base, chip_base, inv_freq = self.clock_sync.get_time_translation()
        freq_conv = float(LDC1612_FREQ) / (1<<28)
        # Process every message in raw_samples
        count = seq = 0
        samples = [None] * (len(raw_samples) * SAMPLES_PER_BLOCK)
        for params in raw_samples:
            seq_diff = (params['sequence'] - last_sequence) & 0xffff
            seq_diff -= (seq_diff & 0x8000) << 1
            seq = last_sequence + seq_diff
            d = bytearray(params['data'])
            msg_cdiff = seq * SAMPLES_PER_BLOCK - chip_base
            for i in range(len(d) // BYTES_PER_SAMPLE):
                v = d[i*BYTES_PER_SAMPLE:(i+1)*BYTES_PER_SAMPLE]
                if v[0] & 0xf0:
                    self.last_error_count += 1
                val = ((v[0] & 0x0f) << 24) | (v[1] << 16) | (v[2] << 8) | v[3]
                ptime = round(time_base + (msg_cdiff + i) * inv_freq, 6)
                samples[count] = (ptime, round(freq_conv * val, 3))
                count += 1
        self.clock_sync.set_last_chip_clock(seq * SAMPLES_PER_BLOCK + i)
        del samples[count:]
        return samples
    # Start, stop, and process message batches
    def _start_measurements(self):
        # In case of miswiring, testing LDC1612 device ID prevents treating
        # noise or wrong signal as a correctly initialized device
        manuf_id = self.read_reg(REG_MANUFACTURER_ID)
        dev_id = self.read_reg(REG_DEVICE_ID)
        if manuf_id != LDC1612_MANUF_ID or dev_id != LDC1612_DEV_ID:
            raise self.printer.command_error(
                "Invalid ldc1612 id (got %x,%x vs %x,%x).\n"
                "This is generally indicative of connection problems\n"
                "(e.g. faulty wiring) or a faulty ldc1612 chip."
                % (manuf_id, dev_id, LDC1612_MANUF_ID, LDC1612_DEV_ID))
        # Setup chip in requested query rate
        rcount0 = LDC1612_FREQ / (16. * (self.data_rate - 4))
        self.set_reg(REG_RCOUNT0, int(rcount0 + 0.5))
        self.set_reg(REG_OFFSET0, 0)
        self.set_reg(REG_SETTLECOUNT0, int(SETTLETIME*LDC1612_FREQ/16. + .5))
        self.set_reg(REG_CLOCK_DIVIDERS0, (1 << 12) | 1)
        self.set_reg(REG_ERROR_CONFIG, (0x1f << 11) | 1)
        self.set_reg(REG_MUX_CONFIG, 0x0208 | DEGLITCH)
        self.set_reg(REG_CONFIG, 0x001 | (1<<12) | (1<<10) | (1<<9))
        self.set_reg(REG_DRIVE_CURRENT0, self.dccal.get_drive_current() << 11)
        # Start bulk reading
        self.bulk_queue.clear_samples()
        rest_ticks = self.mcu.seconds_to_clock(0.5 / self.data_rate)
        self.query_ldc1612_cmd.send([self.oid, rest_ticks])
        logging.info("LDC1612 starting '%s' measurements", self.name)
        # Initialize clock tracking
        self.clock_updater.note_start()
        self.last_error_count = 0
    def _finish_measurements(self):
        # Halt bulk reading
        self.query_ldc1612_cmd.send_wait_ack([self.oid, 0])
        self.bulk_queue.clear_samples()
        logging.info("LDC1612 finished '%s' measurements", self.name)
    def _process_batch(self, eventtime):
        self.clock_updater.update_clock()
        raw_samples = self.bulk_queue.pull_samples()
        if not raw_samples:
            return {}
        samples = self._extract_samples(raw_samples)
        if not samples:
            return {}
        return {'data': samples, 'errors': self.last_error_count,
                'overflows': self.clock_updater.get_last_overflows()}
