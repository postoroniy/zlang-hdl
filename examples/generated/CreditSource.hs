{-# LANGUAGE DataKinds #-}
{-# LANGUAGE DeriveAnyClass #-}
{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}

module CreditSource where

import Clash.Prelude
import GHC.Generics (Generic)
import qualified Clash.Verification as Verification

data ZLangCreditForward a = ZLangCreditForward
  { zlangCreditPayload :: a
  , zlangCreditSend :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

data ZLangCreditReturn = ZLangCreditReturn
  { zlangCreditReturnPulse :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

createDomain vSystem{vName="ZLangSystem", vResetKind=Synchronous}

circuit :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Bit) -> Signal ZLangSystem ZLangCreditReturn -> Signal ZLangSystem (ZLangCreditForward (Unsigned 8))
circuit payload_data request tx_return_input = ZLangCreditForward <$> tx_payload <*> tx_send_checked
 where
  reset_active = unsafeToActiveHigh hasReset
  tx_return = zlangCreditReturnPulse <$> tx_return_input
  tx_payload = payload_data
  tx_send_request = request
  tx_credits = register (2 :: Unsigned 2) tx_credits_next
  tx_send = (\request credits resetActive -> if resetActive || credits == 0 then low else request) <$> tx_send_request <*> tx_credits <*> reset_active
  tx_transfer = tx_send
  tx_no_underflow = (\sent credits -> sent == low || credits > 0) <$> tx_send <*> tx_credits
  tx_no_overflow = (\returned sent credits resetActive -> resetActive || returned == low || sent == high || credits < (2 :: Unsigned 2)) <$> tx_return <*> tx_send <*> tx_credits <*> reset_active
  tx_credits_next = (\credits sent returned -> case (sent == high, returned == high) of { (True, False) -> credits - 1; (False, True) -> if credits < (2 :: Unsigned 2) then credits + 1 else credits; _ -> credits }) <$> tx_credits <*> tx_send <*> tx_return
  tx_send_checked = Verification.checkI "tx_no_underflow" Verification.AutoRenderAs (Verification.assert tx_no_underflow) . Verification.checkI "tx_no_overflow" Verification.AutoRenderAs (Verification.assert tx_no_overflow) $ tx_send

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Bit) -> Signal ZLangSystem ZLangCreditReturn -> Signal ZLangSystem (ZLangCreditForward (Unsigned 8))
topEntity clk rst payload_data request tx_return_input = exposeClockResetEnable circuit clk rst enableGen payload_data request tx_return_input

{-# ANN topEntity
  (Synthesize
    { t_name = "CreditSource"
    , t_inputs = [PortName "clk", PortName "rst", PortName "payload_data", PortName "request", PortName "tx_return"]
    , t_output = PortProduct "tx" [PortName "payload", PortName "send"]
    }) #-}
