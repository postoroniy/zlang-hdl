{-# LANGUAGE DataKinds #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE DeriveAnyClass #-}
{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE NoImplicitPrelude #-}

module CdcAsyncFifo where

import Clash.Explicit.Prelude
import qualified Clash.Explicit.Signal as Explicit
import qualified Clash.Explicit.Synchronizer as Synchronizer
import GHC.Generics (Generic)

data ZLangReadyValidForward a = ZLangReadyValidForward
  { zlangRvPayload :: a
  , zlangRvValid :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

data ZLangReadyValidBackward = ZLangReadyValidBackward
  { zlangRvReady :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

createDomain vSystem{vName="SourceClockDomain", vResetKind=Synchronous}
createDomain vSystem{vName="DestinationClockDomain", vResetKind=Synchronous}

topEntity :: Clock SourceClockDomain -> Reset SourceClockDomain -> Clock DestinationClockDomain -> Reset DestinationClockDomain -> Signal SourceClockDomain (ZLangReadyValidForward (Unsigned 8)) -> Signal DestinationClockDomain ZLangReadyValidBackward -> (Signal SourceClockDomain ZLangReadyValidBackward, Signal DestinationClockDomain (ZLangReadyValidForward (Unsigned 8)))
topEntity source_clock source_reset destination_clock destination_reset source destination_backward = (ZLangReadyValidBackward <$> source_ready, ZLangReadyValidForward <$> destination_payload <*> destination_valid)
 where
  source_payload = zlangRvPayload <$> source
  source_valid = zlangRvValid <$> source
  destination_ready = zlangRvReady <$> destination_backward
  source_reset_active = unsafeToActiveHigh source_reset
  destination_reset_active = unsafeToActiveHigh destination_reset
  (destination_payload, destination_empty, source_full) = Synchronizer.asyncFIFOSynchronizer (SNat @2) source_clock destination_clock source_reset destination_reset enableGen enableGen destination_read source_write
  source_ready = (\full resetActive -> if resetActive || full then low else high) <$> source_full <*> source_reset_active
  source_write = (\payload valid ready -> if valid == high && ready == high then Just payload else Nothing) <$> source_payload <*> source_valid <*> source_ready
  destination_valid = (\empty resetActive -> if resetActive || empty then low else high) <$> destination_empty <*> destination_reset_active
  destination_read = (\valid ready -> valid == high && ready == high) <$> destination_valid <*> destination_ready

{-# ANN topEntity
  (Synthesize
    { t_name = "CdcAsyncFifo"
    , t_inputs = [PortName "source_clock", PortName "source_reset", PortName "destination_clock", PortName "destination_reset", PortProduct "source" [PortName "payload", PortName "valid"], PortName "destination_ready"]
    , t_output = PortProduct "" [PortName "source_ready", PortProduct "destination" [PortName "payload", PortName "valid"]]
    }) #-}
