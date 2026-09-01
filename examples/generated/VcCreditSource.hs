{-# LANGUAGE DataKinds #-}
{-# LANGUAGE DeriveAnyClass #-}
{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}
module VcCreditSource where

import Clash.Prelude
import GHC.Generics (Generic)
import qualified Clash.Verification as Verification

data ZLangVcCreditForward vc a = ZLangVcCreditForward
  { zlangVcCreditPayload :: a
  , zlangVcCreditVc :: vc
  , zlangVcCreditSend :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

data ZLangVcCreditReturn vc = ZLangVcCreditReturn
  { zlangVcCreditReturnPulse :: Bit
  , zlangVcCreditReturnVc :: vc
  } deriving (Generic, NFDataX, Show, Eq)

createDomain vSystem{vName="ZLangSystem", vResetKind=Synchronous}

circuit :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 1) -> Signal ZLangSystem (Bit) -> Signal ZLangSystem (ZLangVcCreditReturn (Unsigned 1)) -> Signal ZLangSystem (ZLangVcCreditForward (Unsigned 1) (Unsigned 8))
circuit payload channel request tx_return_input = ZLangVcCreditForward <$> tx_payload <*> tx_vc <*> tx_send_checked
 where
  reset_active = unsafeToActiveHigh hasReset
  tx_return = zlangVcCreditReturnPulse <$> tx_return_input
  tx_return_vc = zlangVcCreditReturnVc <$> tx_return_input
  tx_payload = payload
  tx_vc = channel
  tx_send_request = request
  tx_credits_0 = register (2 :: Unsigned 2) tx_credits_0_next
  tx_credits_1 = register (2 :: Unsigned 2) tx_credits_1_next
  tx_credits = bundle (tx_credits_0 :> tx_credits_1 :> Nil)
  tx_can_send = (\vc credits_0 credits_1 -> case vc of { 0 -> credits_0 > 0; 1 -> credits_1 > 0; _ -> False }) <$> tx_vc <*> tx_credits_0 <*> tx_credits_1
  tx_send = (\requested allowed resetActive -> if not resetActive && requested == high && allowed then high else low) <$> tx_send_request <*> tx_can_send <*> reset_active
  tx_transfer = tx_send
  tx_sent_0 = (\sent vc -> sent == high && vc == 0) <$> tx_send <*> tx_vc
  tx_returned_0 = (\returned vc -> returned == high && vc == 0) <$> tx_return <*> tx_return_vc
  tx_no_overflow_0 = (\returned sent credits resetActive -> resetActive || not returned || sent || credits < (2 :: Unsigned 2)) <$> tx_returned_0 <*> tx_sent_0 <*> tx_credits_0 <*> reset_active
  tx_credits_0_next = (\credits sent returned -> case (sent, returned) of { (True, False) -> if credits > 0 then credits - 1 else credits; (False, True) -> if credits < (2 :: Unsigned 2) then credits + 1 else credits; _ -> credits }) <$> tx_credits_0 <*> tx_sent_0 <*> tx_returned_0
  tx_sent_1 = (\sent vc -> sent == high && vc == 1) <$> tx_send <*> tx_vc
  tx_returned_1 = (\returned vc -> returned == high && vc == 1) <$> tx_return <*> tx_return_vc
  tx_no_overflow_1 = (\returned sent credits resetActive -> resetActive || not returned || sent || credits < (2 :: Unsigned 2)) <$> tx_returned_1 <*> tx_sent_1 <*> tx_credits_1 <*> reset_active
  tx_credits_1_next = (\credits sent returned -> case (sent, returned) of { (True, False) -> if credits > 0 then credits - 1 else credits; (False, True) -> if credits < (2 :: Unsigned 2) then credits + 1 else credits; _ -> credits }) <$> tx_credits_1 <*> tx_sent_1 <*> tx_returned_1
  tx_send_checked = Verification.checkI "tx_vc_0_no_overflow" Verification.AutoRenderAs (Verification.assert tx_no_overflow_0) $ Verification.checkI "tx_vc_1_no_overflow" Verification.AutoRenderAs (Verification.assert tx_no_overflow_1) $ tx_send

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 1) -> Signal ZLangSystem (Bit) -> Signal ZLangSystem (ZLangVcCreditReturn (Unsigned 1)) -> Signal ZLangSystem (ZLangVcCreditForward (Unsigned 1) (Unsigned 8))
topEntity clk rst payload channel request tx_return_input = exposeClockResetEnable circuit clk rst enableGen payload channel request tx_return_input

{-# ANN topEntity
  (Synthesize
    { t_name = "VcCreditSource"
    , t_inputs = [PortName "clk", PortName "rst", PortName "payload", PortName "channel", PortName "request", PortProduct "tx" [PortName "return", PortName "return_vc"]]
    , t_output = PortProduct "tx" [PortName "payload", PortName "vc", PortName "send"]
    }) #-}
