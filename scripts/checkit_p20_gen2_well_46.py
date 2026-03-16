import json
from opentrons import protocol_api, types

metadata = {
    "protocolName": "checkit p20 gen2 well 4-6",
    "description": "20uL check it 4-6",
    "created": "2026-03-14T21:44:13.987Z",
    "internalAppBuildDate": "Wed, 04 Mar 2026 17:13:57 GMT",
    "lastModified": "2026-03-14T23:24:39.849Z",
    "protocolDesigner": "8.9.0",
    "source": "Protocol Designer",
}

requirements = {"robotType": "OT-2", "apiLevel": "2.27"}

def run(protocol: protocol_api.ProtocolContext) -> None:
    # Load Labware:
    tip_rack_1 = protocol.load_labware(
        "opentrons_96_filtertiprack_20ul",
        location="2",
        namespace="opentrons",
        version=1,
    )
    well_plate_1 = protocol.load_labware_from_definition(
        CUSTOM_LABWARE["custom_beta/checkit_8_wellplate_20ul/1"],
        location="1",
    )

    # Load Pipettes:
    pipette_left = protocol.load_instrument("p20_single_gen2", "left")

    # Define Liquids:
    liquid_1 = protocol.define_liquid(
        "water",
        display_color="#b925ff",
    )

    # Load Liquids:
    well_plate_1.load_liquid(
        wells=["A2"],
        liquid=liquid_1,
        volume=50,
    )

    # PROTOCOL STEPS

    # Step 1: transfer
    pipette_left.distribute_with_liquid_class(
        volume=2,
        source=[well_plate_1["A2"]],
        dest=[well_plate_1["A2"],well_plate_1["D1"], well_plate_1["E1"], well_plate_1["F1"]],
        new_tip="once",
        trash_location=protocol.fixed_trash,
        keep_last_tip=True,
        tip_racks=[tip_rack_1],
        liquid_class=protocol.define_liquid_class(
            name="distribute_step_1",
            properties={"p20_single_gen2": {"opentrons/opentrons_96_filtertiprack_20ul/1": {
                "aspirate": {
                    "aspirate_position": {
                        "offset": {"x": 0, "y": 0, "z": 1},
                        "position_reference": "well-bottom",
                    },
                    "flow_rate_by_volume": [(0, 3.7)],
                    "pre_wet": True,
                    "correction_by_volume": [(0, 0)],
                    "delay": {"enabled": True, "duration": 2},
                    "mix": {"enabled": False},
                    "submerge": {
                        "delay": {"enabled": False},
                        "speed": 125,
                        "start_position": {
                            "offset": {"x": 0, "y": 0, "z": 2},
                            "position_reference": "well-top",
                        },
                    },
                    "retract": {
                        "air_gap_by_volume": [(0, 0)],
                        "delay": {"enabled": False},
                        "end_position": {
                            "offset": {"x": 0, "y": 0, "z": 2},
                            "position_reference": "well-top",
                        },
                        "speed": 125,
                        "touch_tip": {"enabled": False},
                    },
                },
                "dispense": {
                    "dispense_position": {
                        "offset": {"x": 0, "y": 0, "z": 3},
                        "position_reference": "well-bottom",
                    },
                    "flow_rate_by_volume": [(0, 3.7)],
                    "delay": {"enabled": True, "duration": 1},
                    "submerge": {
                        "delay": {"enabled": False},
                        "speed": 125,
                        "start_position": {
                            "offset": {"x": 0, "y": 0, "z": 2},
                            "position_reference": "well-top",
                        },
                    },
                    "retract": {
                        "air_gap_by_volume": [(0, 0)],
                        "delay": {"enabled": False},
                        "end_position": {
                            "offset": {"x": 0, "y": 0, "z": 2},
                            "position_reference": "well-top",
                        },
                        "speed": 125,
                        "touch_tip": {"enabled": False},
                        "blowout": {"enabled": True, "location": "trash", "flow_rate": 3.7},
                    },
                    "correction_by_volume": [(0, 0)],
                    "push_out_by_volume": [(0, 0)],
                    "mix": {"enabled": False},
                },
                "multi_dispense": {
                    "dispense_position": {
                        "offset": {"x": 0, "y": 0, "z": 3},
                        "position_reference": "well-bottom",
                    },
                    "flow_rate_by_volume": [(0, 3.7)],
                    "delay": {"enabled": True, "duration": 1},
                    "submerge": {
                        "delay": {"enabled": False},
                        "speed": 125,
                        "start_position": {
                            "offset": {"x": 0, "y": 0, "z": 2},
                            "position_reference": "well-top",
                        },
                    },
                    "retract": {
                        "air_gap_by_volume": [(0, 0)],
                        "delay": {"enabled": False},
                        "end_position": {
                            "offset": {"x": 0, "y": 0, "z": 2},
                            "position_reference": "well-top",
                        },
                        "speed": 125,
                        "touch_tip": {"enabled": False},
                        "blowout": {"enabled": True, "location": "trash", "flow_rate": 3.7},
                    },
                    "correction_by_volume": [(0, 0)],
                    "conditioning_by_volume": [(0, 0)],
                    "disposal_by_volume": [(0, 4)],
                },
            }}},
        ),
    )
    pipette_left.drop_tip()

CUSTOM_LABWARE = json.loads("""{"custom_beta/checkit_8_wellplate_20ul/1":{"brand":{"brand":"Checkit","brandId":[]},"wells":{"A1":{"x":114.38,"y":74.28,"z":7.5,"depth":4.4,"shape":"circular","diameter":5,"totalLiquidVolume":50},"A2":{"x":14.38,"y":74.28,"z":9.75,"depth":12.85,"shape":"rectangular","xDimension":8.2,"yDimension":10,"totalLiquidVolume":50},"B1":{"x":114.38,"y":65.28,"z":7.5,"depth":4.4,"shape":"circular","diameter":5,"totalLiquidVolume":50},"C1":{"x":114.38,"y":56.28,"z":7.5,"depth":4.4,"shape":"circular","diameter":5,"totalLiquidVolume":50},"D1":{"x":114.38,"y":47.28,"z":7.5,"depth":4.4,"shape":"circular","diameter":5,"totalLiquidVolume":50},"E1":{"x":114.38,"y":38.28,"z":7.5,"depth":4.4,"shape":"circular","diameter":5,"totalLiquidVolume":50},"F1":{"x":114.38,"y":29.28,"z":7.5,"depth":4.4,"shape":"circular","diameter":5,"totalLiquidVolume":50},"G1":{"x":114.38,"y":20.28,"z":7.5,"depth":4.4,"shape":"circular","diameter":5,"totalLiquidVolume":50},"H1":{"x":114.38,"y":11.28,"z":7.5,"depth":4.4,"shape":"circular","diameter":5,"totalLiquidVolume":50}},"groups":[{"wells":["A1","B1","C1","D1","E1","F1","G1","H1"],"metadata":{"wellBottomShape":"u"}},{"wells":["A2"],"metadata":{"wellBottomShape":"u"}}],"version":1,"metadata":{"tags":[],"displayName":"Checkit 8 Well Plate 20 µL","displayCategory":"wellPlate","displayVolumeUnits":"µL"},"ordering":[["A1","B1","C1","D1","E1","F1","G1","H1"],["A2"]],"namespace":"custom_beta","dimensions":{"xDimension":127.76,"yDimension":85.48,"zDimension":12.2},"parameters":{"format":"irregular","quirks":[],"loadName":"checkit_8_wellplate_20ul","isTiprack":false,"isMagneticModuleCompatible":false},"schemaVersion":2,"cornerOffsetFromSlot":{"x":0,"y":0,"z":0}}}""")

DESIGNER_APPLICATION = """{"robot":{"model":"OT-2 Standard"},"designerApplication":{"name":"opentrons/protocol-designer","version":"8.8.0","data":{"pipetteTiprackAssignments":{"8f70a7c6-5f57-42f1-98ee-820a59bbc366":["opentrons/opentrons_96_filtertiprack_20ul/1"]},"dismissedWarnings":{"form":[],"timeline":[]},"ingredients":{"0":{"displayName":"water","displayColor":"#b925ff","description":null,"liquidGroupId":"0"}},"ingredLocations":{"d38f9567-9fd7-4aac-a3d4-47f683c38c49:custom_beta/checkit_8_wellplate_20ul/1":{"A2":{"0":{"volume":50}}}},"savedStepForms":{"__INITIAL_DECK_SETUP_STEP__":{"stepType":"manualIntervention","id":"__INITIAL_DECK_SETUP_STEP__","labwareLocationUpdate":{"9d5f6e98-28d9-406b-a50a-1c0c623e22f2:opentrons/opentrons_96_filtertiprack_20ul/1":"2","d38f9567-9fd7-4aac-a3d4-47f683c38c49:custom_beta/checkit_8_wellplate_20ul/1":"1"},"pipetteLocationUpdate":{"8f70a7c6-5f57-42f1-98ee-820a59bbc366":"left"},"moduleLocationUpdate":{},"moduleStateUpdate":{},"trashBinLocationUpdate":{"29c5c77a-23ea-41a3-9613-4370a90396f2:trashBin":"cutout12"},"wasteChuteLocationUpdate":{},"stagingAreaLocationUpdate":{},"gripperLocationUpdate":{}},"a48ba451-0611-4cd6-bbb2-85aa885273f9":{"id":"a48ba451-0611-4cd6-bbb2-85aa885273f9","stepType":"moveLiquid","stepName":"transfer","stepDetails":"","stepNumber":0,"aspirate_airGap_checkbox":false,"aspirate_airGap_volume":null,"aspirate_delay_checkbox":true,"aspirate_delay_seconds":"2","aspirate_flowRate":"3.7","aspirate_labware":"d38f9567-9fd7-4aac-a3d4-47f683c38c49:custom_beta/checkit_8_wellplate_20ul/1","aspirate_mix_checkbox":false,"aspirate_mix_times":"","aspirate_mix_volume":null,"aspirate_mmFromBottom":null,"aspirate_position_reference":"well-bottom","aspirate_retract_delay_seconds":"0","aspirate_retract_mmFromBottom":2,"aspirate_retract_speed":"125","aspirate_retract_x_position":0,"aspirate_retract_y_position":0,"aspirate_retract_position_reference":"well-top","aspirate_submerge_delay_seconds":"0","aspirate_submerge_speed":"125","aspirate_submerge_mmFromBottom":2,"aspirate_submerge_x_position":0,"aspirate_submerge_y_position":0,"aspirate_submerge_position_reference":"well-top","aspirate_touchTip_checkbox":false,"aspirate_touchTip_mmFromTop":null,"aspirate_touchTip_speed":60,"aspirate_touchTip_mmFromEdge":0,"aspirate_wellOrder_first":"t2b","aspirate_wellOrder_second":"l2r","aspirate_wells_grouped":false,"aspirate_wells":["A2"],"aspirate_x_position":0,"aspirate_y_position":0,"blowout_checkbox":false,"blowout_flowRate":"3.7","blowout_location":"29c5c77a-23ea-41a3-9613-4370a90396f2:trashBin","changeTip":"once","conditioning_checkbox":false,"conditioning_volume":null,"dispense_airGap_checkbox":false,"dispense_airGap_volume":"","dispense_delay_checkbox":true,"dispense_delay_seconds":"1","dispense_flowRate":"3.7","dispense_labware":"d38f9567-9fd7-4aac-a3d4-47f683c38c49:custom_beta/checkit_8_wellplate_20ul/1","dispense_mix_checkbox":false,"dispense_mix_times":null,"dispense_mix_volume":null,"dispense_mmFromBottom":3,"dispense_position_reference":"well-bottom","dispense_retract_delay_seconds":"0","dispense_retract_mmFromBottom":2,"dispense_retract_speed":"125","dispense_retract_x_position":0,"dispense_retract_y_position":0,"dispense_retract_position_reference":"well-top","dispense_submerge_delay_seconds":"0","dispense_submerge_speed":"125","dispense_submerge_mmFromBottom":2,"dispense_submerge_x_position":0,"dispense_submerge_y_position":0,"dispense_submerge_position_reference":"well-top","dispense_touchTip_checkbox":false,"dispense_touchTip_mmFromTop":null,"dispense_touchTip_speed":60,"dispense_touchTip_mmFromEdge":0,"dispense_wellOrder_first":"t2b","dispense_wellOrder_second":"l2r","dispense_wells":["D1","E1","F1"],"dispense_x_position":0,"dispense_y_position":0,"disposalVolume_checkbox":true,"disposalVolume_volume":"4","dropTip_location":"29c5c77a-23ea-41a3-9613-4370a90396f2:trashBin","liquidClassesSupported":true,"liquidClass":"none","nozzles":null,"path":"multiDispense","pipette":"8f70a7c6-5f57-42f1-98ee-820a59bbc366","preWetTip":true,"pushOut_checkbox":false,"pushOut_volume":"0","tipRack":"opentrons/opentrons_96_filtertiprack_20ul/1","tip_tracking":"automatic","tiprack_selected":null,"tips_selected":[],"volume":"2"}},"orderedStepIds":["a48ba451-0611-4cd6-bbb2-85aa885273f9"],"pipettes":{"8f70a7c6-5f57-42f1-98ee-820a59bbc366":{"pipetteName":"p20_single_gen2"}},"modules":{},"labware":{"9d5f6e98-28d9-406b-a50a-1c0c623e22f2:opentrons/opentrons_96_filtertiprack_20ul/1":{"displayName":"Opentrons OT-2 96 Filter Tip Rack 20 µL","labwareDefURI":"opentrons/opentrons_96_filtertiprack_20ul/1"},"d38f9567-9fd7-4aac-a3d4-47f683c38c49:custom_beta/checkit_8_wellplate_20ul/1":{"displayName":"Checkit 8 Well Plate 20 µL","labwareDefURI":"custom_beta/checkit_8_wellplate_20ul/1"}}}},"metadata":{"protocolName":"checkit p20 gen2 well 4-6","author":"","description":"20uL check it 4-6","source":"Protocol Designer","created":1773524653987,"lastModified":1773530679849}}"""
