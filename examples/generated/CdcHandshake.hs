{-# LANGUAGE DataKinds #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE DeriveAnyClass #-}
{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE NoImplicitPrelude #-}

module CdcHandshake where

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
  source_ready = (\request acknowledge resetActive -> if resetActive || request /= acknowledge then low else high) <$> source_request <*> synchronized_acknowledge <*> source_reset_active
  source_transfer = (\valid ready -> valid .&. ready) <$> source_valid <*> source_ready
  source_data = register source_clock source_reset enableGen (0 :: Unsigned 8) source_data_next
  source_data_next = (\held payload transferred -> if transferred == high then payload else held) <$> source_data <*> source_payload <*> source_transfer
  source_request = register source_clock source_reset enableGen low source_request_next
  source_request_next = (\request transferred -> if transferred == high then if request == high then low else high else request) <$> source_request <*> source_transfer
  synchronized_acknowledge = Synchronizer.dualFlipFlopSynchronizer destination_clock source_clock source_reset enableGen low destination_acknowledge
  synchronized_request = Synchronizer.dualFlipFlopSynchronizer source_clock destination_clock destination_reset enableGen low source_request
  crossed_data = Explicit.unsafeSynchronizer source_clock destination_clock source_data
  destination_valid = (\request acknowledge resetActive -> if resetActive || request == acknowledge then low else high) <$> synchronized_request <*> destination_acknowledge <*> destination_reset_active
  destination_payload = crossed_data
  destination_transfer = (\valid ready -> valid .&. ready) <$> destination_valid <*> destination_ready
  destination_acknowledge = register destination_clock destination_reset enableGen low destination_acknowledge_next
  destination_acknowledge_next = (\acknowledge request transferred -> if transferred == high then request else acknowledge) <$> destination_acknowledge <*> synchronized_request <*> destination_transfer

{-# ANN topEntity
  (Synthesize
    { t_name = "CdcHandshake"
    , t_inputs = [PortName "source_clock", PortName "source_reset", PortName "destination_clock", PortName "destination_reset", PortProduct "source" [PortName "payload", PortName "valid"], PortName "destination_ready"]
    , t_output = PortProduct "" [PortName "source_ready", PortProduct "destination" [PortName "payload", PortName "valid"]]
    }) #-}
