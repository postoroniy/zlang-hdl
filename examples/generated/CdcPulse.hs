{-# LANGUAGE DataKinds #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}

module CdcPulse where

import Clash.Explicit.Prelude
import qualified Clash.Explicit.Signal as Explicit
import qualified Clash.Explicit.Synchronizer as Synchronizer

createDomain vSystem{vName="SourceClockDomain", vResetKind=Synchronous}
createDomain vSystem{vName="DestinationClockDomain", vResetKind=Synchronous}

topEntity :: Clock SourceClockDomain -> Reset SourceClockDomain -> Clock DestinationClockDomain -> Reset DestinationClockDomain -> Signal SourceClockDomain (Bit) -> Signal DestinationClockDomain (Bit)
topEntity source_clock source_reset destination_clock destination_reset pulse = crossed_pulse
 where
  source_toggle = register source_clock source_reset enableGen low source_toggle_next
  source_toggle_next = (\toggle pulse -> if pulse == high then if toggle == high then low else high else toggle) <$> source_toggle <*> pulse
  synchronized_toggle = Synchronizer.dualFlipFlopSynchronizer source_clock destination_clock destination_reset enableGen low source_toggle
  previous_toggle = register destination_clock destination_reset enableGen low synchronized_toggle
  crossed_pulse = xor <$> synchronized_toggle <*> previous_toggle

{-# ANN topEntity
  (Synthesize
    { t_name = "CdcPulse"
    , t_inputs = [PortName "source_clock", PortName "source_reset", PortName "destination_clock", PortName "destination_reset", PortName "pulse"]
    , t_output = PortName "crossed_pulse"
    }) #-}
