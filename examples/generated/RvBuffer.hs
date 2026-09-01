{-# LANGUAGE DataKinds #-}
{-# LANGUAGE DeriveAnyClass #-}
{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}

module RvBuffer where

import Clash.Prelude
import GHC.Generics (Generic)

data ZLangReadyValidForward a = ZLangReadyValidForward
  { zlangRvPayload :: a
  , zlangRvValid :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

data ZLangReadyValidBackward = ZLangReadyValidBackward
  { zlangRvReady :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

createDomain vSystem{vName="ZLangSystem", vResetKind=Synchronous}

circuit :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem (ZLangReadyValidForward (Unsigned 8)) -> Signal ZLangSystem ZLangReadyValidBackward -> (Signal ZLangSystem ZLangReadyValidBackward, Signal ZLangSystem (ZLangReadyValidForward (Unsigned 8)))
circuit rx tx_backward = (ZLangReadyValidBackward <$> rx_ready, ZLangReadyValidForward <$> tx_payload <*> tx_valid)
 where
  reset_active = unsafeToActiveHigh hasReset
  rx_payload = zlangRvPayload <$> rx
  rx_valid = zlangRvValid <$> rx
  tx_ready = zlangRvReady <$> tx_backward
  rx_tx_buffer_count = register (0 :: Unsigned 2) rx_tx_buffer_count_next
  rx_tx_buffer_slots = register (repeat (deepErrorX "empty connection buffer") :: Vec 2 (Unsigned 8)) rx_tx_buffer_slots_next
  rx_tx_buffer_count_next = (\count enqueued dequeued -> case (enqueued == high, dequeued == high) of { (True, False) -> count + 1; (False, True) -> count - 1; _ -> count }) <$> rx_tx_buffer_count <*> rx_tx_buffer_enqueue <*> rx_tx_buffer_dequeue
  rx_tx_buffer_slots_next = (\slots count enqueued dequeued payload -> case (enqueued == high, dequeued == high) of { (True, False) -> replace (fromIntegral count) payload slots; (False, True) -> slots <<+ deepErrorX "empty connection buffer"; (True, True) -> replace (fromIntegral (count - 1)) payload (slots <<+ deepErrorX "empty connection buffer"); _ -> slots }) <$> rx_tx_buffer_slots <*> rx_tx_buffer_count <*> rx_tx_buffer_enqueue <*> rx_tx_buffer_dequeue <*> rx_payload
  rx_ready = (\count dequeued resetActive -> if resetActive || (count >= 2 && dequeued == low) then low else high) <$> rx_tx_buffer_count <*> rx_tx_buffer_dequeue <*> reset_active
  rx_tx_buffer_enqueue = (\valid ready -> valid .&. ready) <$> rx_valid <*> rx_ready
  tx_payload = head <$> rx_tx_buffer_slots
  tx_valid = (\count resetActive -> if resetActive || count == 0 then low else high) <$> rx_tx_buffer_count <*> reset_active
  rx_tx_buffer_dequeue = (\valid ready -> valid .&. ready) <$> tx_valid <*> tx_ready
  rx_transfer = (\valid ready -> valid .&. ready) <$> rx_valid <*> rx_ready
  tx_transfer = (\valid ready -> valid .&. ready) <$> tx_valid <*> tx_ready

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> Signal ZLangSystem (ZLangReadyValidForward (Unsigned 8)) -> Signal ZLangSystem ZLangReadyValidBackward -> (Signal ZLangSystem ZLangReadyValidBackward, Signal ZLangSystem (ZLangReadyValidForward (Unsigned 8)))
topEntity clk rst rx tx_backward = exposeClockResetEnable circuit clk rst enableGen rx tx_backward

{-# ANN topEntity
  (Synthesize
    { t_name = "RvBuffer"
    , t_inputs = [PortName "clk", PortName "rst", PortProduct "rx" [PortName "payload", PortName "valid"], PortName "tx_ready"]
    , t_output = PortProduct "" [PortName "rx_ready", PortProduct "tx" [PortName "payload", PortName "valid"]]
    }) #-}
