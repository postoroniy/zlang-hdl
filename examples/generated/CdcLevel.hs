{-# LANGUAGE DataKinds #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}

module CdcLevel where

import Clash.Explicit.Prelude
import qualified Clash.Explicit.Signal as Explicit
import qualified Clash.Explicit.Synchronizer as Synchronizer

createDomain vSystem{vName="SourceClockDomain", vResetKind=Synchronous}
createDomain vSystem{vName="DestinationClockDomain", vResetKind=Synchronous}

topEntity :: Clock SourceClockDomain -> Reset SourceClockDomain -> Clock DestinationClockDomain -> Reset DestinationClockDomain -> Signal SourceClockDomain (Bit) -> Signal DestinationClockDomain (Bit)
topEntity source_clock source_reset destination_clock destination_reset level = Synchronizer.dualFlipFlopSynchronizer source_clock destination_clock destination_reset enableGen low level

{-# ANN topEntity
  (Synthesize
    { t_name = "CdcLevel"
    , t_inputs = [PortName "source_clock", PortName "source_reset", PortName "destination_clock", PortName "destination_reset", PortName "level"]
    , t_output = PortName "synced"
    }) #-}
