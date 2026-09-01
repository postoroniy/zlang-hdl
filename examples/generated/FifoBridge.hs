{-# LANGUAGE DataKinds #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE DeriveAnyClass #-}
{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE NoImplicitPrelude #-}

module FifoBridge where

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
  rx_transfer = (\valid ready -> valid .&. ready) <$> rx_valid <*> rx_ready
  tx_transfer = (\valid ready -> valid .&. ready) <$> tx_valid <*> tx_ready
  queue_data_request = (\value_0 -> value_0) <$> rx_payload
  queue_push_request = (\value_0 -> value_0) <$> rx_transfer
  queue_pop_request = (\value_0 -> value_0) <$> tx_transfer
  queue_count = register (0 :: Unsigned 3) queue_count_next
  queue_slots = register (repeat (deepErrorX "empty FIFO queue") :: Vec 4 (Unsigned 8)) queue_slots_next
  queue_empty = (\count -> if count == 0 then high else low) <$> queue_count
  queue_full = (\count -> if count >= 4 then high else low) <$> queue_count
  queue_valid = (\count resetActive -> if resetActive || count == 0 then low else high) <$> queue_count <*> reset_active
  queue_dequeue = (\requested count resetActive -> if not resetActive && requested == high && count > 0 then high else low) <$> queue_pop_request <*> queue_count <*> reset_active
  queue_ready = (\count dequeued resetActive -> if not resetActive && (count < 4 || dequeued == high) then high else low) <$> queue_count <*> queue_dequeue <*> reset_active
  queue_enqueue = (\requested ready -> requested .&. ready) <$> queue_push_request <*> queue_ready
  queue_overflow = (\requested count dequeued resetActive -> if not resetActive && requested == high && count >= 4 && dequeued == low then high else low) <$> queue_push_request <*> queue_count <*> queue_dequeue <*> reset_active
  queue_underflow = (\requested count resetActive -> if not resetActive && requested == high && count == 0 then high else low) <$> queue_pop_request <*> queue_count <*> reset_active
  queue_front = head <$> queue_slots
  queue_count_next = (\count enqueued dequeued -> case (enqueued == high, dequeued == high) of { (True, False) -> count + 1; (False, True) -> count - 1; _ -> count }) <$> queue_count <*> queue_enqueue <*> queue_dequeue
  queue_slots_next = (\slots count enqueued dequeued payload -> case (enqueued == high, dequeued == high) of { (True, False) -> replace (fromIntegral count) payload slots; (False, True) -> slots <<+ deepErrorX "empty FIFO queue"; (True, True) -> replace (fromIntegral (count - 1)) payload (slots <<+ deepErrorX "empty FIFO queue"); _ -> slots }) <$> queue_slots <*> queue_count <*> queue_enqueue <*> queue_dequeue <*> queue_data_request
  rx_ready = queue_ready
  tx_payload = queue_front
  tx_valid = queue_valid

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> Signal ZLangSystem (ZLangReadyValidForward (Unsigned 8)) -> Signal ZLangSystem ZLangReadyValidBackward -> (Signal ZLangSystem ZLangReadyValidBackward, Signal ZLangSystem (ZLangReadyValidForward (Unsigned 8)))
topEntity clk rst rx tx_backward = exposeClockResetEnable circuit clk rst enableGen rx tx_backward

{-# ANN topEntity
  (Synthesize
    { t_name = "FifoBridge"
    , t_inputs = [PortName "clk", PortName "rst", PortProduct "rx" [PortName "payload", PortName "valid"], PortName "tx_ready"]
    , t_output = PortProduct "" [PortName "rx_ready", PortProduct "tx" [PortName "payload", PortName "valid"]]
    }) #-}
