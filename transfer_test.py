from opentrons import protocol_api

metadata = {
    "protocolName": "A1 to H12 with Left and Right P300",
    "author": "Codex",
    "description": "Aspirate 20 uL from A1 and dispense to H12 using both left and right P300 pipettes.",
}

requirements = {
    "robotType": "OT-2",
    "apiLevel": "2.15",
}


def run(protocol: protocol_api.ProtocolContext):
    # Labware
    plate = protocol.load_labware("nest_96_wellplate_200ul_flat", "1")
    tiprack_300 = protocol.load_labware("opentrons_96_tiprack_300ul", "8")  # tip box at slot 8

    # Instruments
    left_p300 = protocol.load_instrument("p300_single", mount="left")
    right_p300 = protocol.load_instrument("p300_single", mount="right")

    source = plate["A1"]
    dest = plate["H12"]
    vol = 20

    # Left pipette transfer
    left_p300.pick_up_tip(tiprack_300["A1"])
    left_p300.aspirate(vol, source.bottom(1))
    left_p300.dispense(vol, dest.bottom(1))
    left_p300.blow_out(dest.top())
    left_p300.drop_tip()

    # Right pipette transfer
    right_p300.pick_up_tip(tiprack_300["A2"])
    right_p300.aspirate(vol, source.bottom(1))
    right_p300.dispense(vol, dest.bottom(1))
    right_p300.blow_out(dest.top())
    right_p300.drop_tip()
