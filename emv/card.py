from __future__ import division, absolute_import, print_function, unicode_literals
from .transmission import TransmissionProtocol
from .protocol.structures import TLV
from .protocol.data import Tag
from .protocol.response import WarningResponse, ErrorResponse
from .protocol.command import (
    SelectCommand,
    ReadCommand,
    GetDataCommand,
    GetProcessingOptions,
    VerifyCommand,
)
from .exc import InvalidPINException, MissingAppException
from .util import decode_int
from .cap import get_arqc_req, get_cap_value


class Card(object):
    """ High-level card manipulation API """

    def __init__(self, connection):
        self.tp = TransmissionProtocol(connection)

    def get_mf(self):
        """ Get the master file (MF). """
        return self.tp.exchange(SelectCommand(file_identifier=[0x3F, 0x00]))

    def list_applications(self):
        """ List applications on the card """
        res = self.tp.exchange(SelectCommand("1PAY.SYS.DDF01"))
        sfi = res.data[Tag.FCI][Tag.FCI_PROP][Tag.SFI][0]
        apps = []

        # Apps may be stored in different records, so iterate through records
        # until we hit an error
        for i in range(1, 31):
            try:
                res = self.read_record(i, sfi)
            except ErrorResponse:
                break
            new_apps = res.data[Tag.RECORD][Tag.APP]
            if type(new_apps) is not list:
                new_apps = [new_apps]
            apps += new_apps
        return apps

    def read_record(self, record_number, sfi=None):
        return self.tp.exchange(ReadCommand(record_number, sfi))

    def select_application(self, app):
        try:
            res = self.tp.exchange(SelectCommand(app))
        except ErrorResponse as e:
            raise MissingAppException(e)
        return res

    def get_data_item(self, item, tag):
        try:
            res = self.tp.exchange(GetDataCommand(item))
            return res.data[tag]
        except ErrorResponse:
            return None

    def get_metadata(self):
        data = {}
        res = self.get_data_item(GetDataCommand.PIN_TRY_COUNT, (0x9F, 0x17))
        if res:
            data["pin_retries"] = res[0]

        res = self.get_data_item(GetDataCommand.ATC, (0x9F, 0x36))
        if res:
            data["atc"] = decode_int(res)

        res = self.get_data_item(GetDataCommand.LAST_ONLINE_ATC, (0x9F, 0x13))
        if res:
            data["last_online_atc"] = decode_int(res)

        return data

    def get_processing_options(self):
        res = self.tp.exchange(GetProcessingOptions())
        if Tag.RMTF1 in res.data:
            # Response template format 1
            return {"AIP": res.data[Tag.RMTF1][:2], "AFL": res.data[Tag.RMTF1][2:]}
        elif Tag.RMTF2 in res.data:
            # Response template format 2
            return {"AIP": res.data[Tag.RMTF2][0x82], "AFL": res.data[Tag.RMTF2][0x94]}

    def get_application_data(self, afl):
        assert len(afl) % 4 == 0
        data = TLV()
        for i in range(0, len(afl), 4):
            sfi = afl[i] >> 3
            start_rec = afl[i + 1]
            end_rec = afl[i + 2]
            # dar = afl[i + 3]
            for i in range(start_rec, end_rec + 1):
                res = self.tp.exchange(ReadCommand(start_rec, sfi))
                data.update(res.data[Tag.RECORD])
        return data

    def verify_pin(self, pin):
        """ Verify the PIN, raising an exception if it fails."""
        res = self.tp.exchange(VerifyCommand(pin))
        if type(res) == WarningResponse:
            raise InvalidPINException(str(res))

        return res

    def generate_cap_value(self, pin, challenge=None, value=None):
        """ Perform a transaction to generate the EMV CAP (Pinsentry) value. """
        apps = self.list_applications()

        if len(apps) == 0:
            raise MissingAppException("No apps on card")

        # We're selecting the last app on the card here, which seems be the correct
        # (bank-specific) one. If this isn't always the case, it may be better to
        # select the app with ADF [A0 00 00 00 03 80 02].
        self.select_application(apps[-1][Tag.ADF_NAME])

        # Get Processing Options starts the transaction on the card and
        # increments the transaction counter.
        opts = self.get_processing_options()

        # Fetch the application data referenced in the processing options.
        # This includes the CDOL data structure which dictates the format
        # of the data passed to the Get Application Cryptogram function.
        app_data = self.get_application_data(opts["AFL"])

        self.verify_pin(pin)

        resp = self.tp.exchange(
            get_arqc_req(app_data, challenge=challenge, value=value)
        )

        # Set default: don't use PAN Sequence Number
        psn = None

        # If the third bit of Issuer Authentication Flags is set then use the PAN Sequence Number
        if app_data[(0x9F, 0x55)][0] & 0x40:
            psn = app_data[Tag.PAN_SN]

        return get_cap_value(resp, app_data[Tag.IPB], psn)
