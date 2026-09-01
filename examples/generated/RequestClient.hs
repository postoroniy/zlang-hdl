{-# LANGUAGE DataKinds #-}
{-# LANGUAGE DeriveAnyClass #-}
{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE FlexibleContexts #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}

module RequestClient where

import Clash.Prelude
import GHC.Generics (Generic)
import qualified Clash.Verification as Verification

data ZLangReadyValidForward a = ZLangReadyValidForward
  { zlangRvPayload :: a
  , zlangRvValid :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

data ZLangReadyValidBackward = ZLangReadyValidBackward
  { zlangRvReady :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

data Request = Request
  { request_id :: Unsigned 2
  , request_data :: Unsigned 8
  } deriving (Generic, NFDataX, Show, Eq)

data Response = Response
  { response_id :: Unsigned 2
  , response_data :: Unsigned 8
  } deriving (Generic, NFDataX, Show, Eq)

zlangContainsId :: (KnownNat n, Eq a) => a -> Vec n Bit -> Vec n a -> Bool
zlangContainsId identifier valids identifiers =
  or (zipWith (\valid slot -> valid == high && slot == identifier) valids identifiers)

zlangInsertId :: KnownNat n => a -> Vec n Bit -> Vec n a -> (Vec n Bit, Vec n a)
zlangInsertId identifier valids identifiers =
  case findIndex (== low) valids of
    Just index -> (replace index high valids, replace index identifier identifiers)
    Nothing -> (valids, identifiers)

zlangRemoveId :: Eq a => a -> Vec n Bit -> Vec n a -> (Vec n Bit, Vec n a)
zlangRemoveId identifier valids identifiers =
  (zipWith (\valid slot -> if valid == high && slot == identifier then low else valid) valids identifiers, identifiers)

zlangUpdateIds :: (KnownNat n, Eq a) => Bit -> a -> Bit -> a -> Vec n Bit -> Vec n a -> (Vec n Bit, Vec n a)
zlangUpdateIds requestTransfer requestId responseTransfer responseId valids identifiers =
  let (afterResponseValid, afterResponseIds) =
        if responseTransfer == high
          then zlangRemoveId responseId valids identifiers
          else (valids, identifiers)
  in if requestTransfer == high
       then zlangInsertId requestId afterResponseValid afterResponseIds
       else (afterResponseValid, afterResponseIds)

createDomain vSystem{vName="ZLangSystem", vResetKind=Synchronous}

circuit :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem (Request) -> Signal ZLangSystem (Bit) -> Signal ZLangSystem (Bit) -> Signal ZLangSystem ZLangReadyValidBackward -> Signal ZLangSystem (ZLangReadyValidForward (Response)) -> (Signal ZLangSystem (Response), Signal ZLangSystem (ZLangReadyValidForward (Request)), Signal ZLangSystem ZLangReadyValidBackward)
circuit request_payload issue accept_response mem_request_backward mem_response_forward = (response_payload, ZLangReadyValidForward <$> mem_request_payload <*> mem_request_valid_checked, ZLangReadyValidBackward <$> mem_response_ready_checked)
 where
  reset_active = unsafeToActiveHigh hasReset
  mem_request_ready = zlangRvReady <$> mem_request_backward
  mem_response_payload = zlangRvPayload <$> mem_response_forward
  mem_response_valid = zlangRvValid <$> mem_response_forward
  mem_request_payload = request_payload
  mem_request_valid_request = issue
  mem_response_ready_request = accept_response
  response_payload = (\value_0 -> value_0) <$> mem_response_payload
  mem_outstanding = register (0 :: Unsigned 2) mem_outstanding_next
  mem_request_id = request_id <$> mem_request_payload
  mem_response_id = response_id <$> mem_response_payload
  mem_ids_valid = register (repeat low :: Vec 2 Bit) mem_ids_valid_next
  mem_ids = register (repeat (0 :: Unsigned 2) :: Vec 2 (Unsigned 2)) mem_ids_next
  mem_duplicate = (\requested ready count identifier valids ids resetActive -> not resetActive && requested == high && ready == high && count < (2 :: Unsigned 2) && zlangContainsId identifier valids ids) <$> mem_request_valid_request <*> mem_request_ready <*> mem_outstanding <*> mem_request_id <*> mem_ids_valid <*> mem_ids <*> reset_active
  mem_missing = (\requested valid count identifier valids ids resetActive -> not resetActive && requested == high && valid == high && count > 0 && not (zlangContainsId identifier valids ids)) <$> mem_response_ready_request <*> mem_response_valid <*> mem_outstanding <*> mem_response_id <*> mem_ids_valid <*> mem_ids <*> reset_active
  mem_request_valid = (\requested count resetActive duplicate -> if resetActive || count >= (2 :: Unsigned 2) || duplicate then low else requested) <$> mem_request_valid_request <*> mem_outstanding <*> reset_active <*> mem_duplicate
  mem_request_transfer = (\valid ready -> valid .&. ready) <$> mem_request_valid <*> mem_request_ready
  mem_response_ready = (\requested count resetActive missing -> if resetActive || count == 0 || missing then low else requested) <$> mem_response_ready_request <*> mem_outstanding <*> reset_active <*> mem_missing
  mem_response_transfer = (\valid ready -> valid .&. ready) <$> mem_response_valid <*> mem_response_ready
  mem_within_limit = (\transfer count -> transfer == low || count < (2 :: Unsigned 2)) <$> mem_request_transfer <*> mem_outstanding
  mem_has_request = (\transfer count -> transfer == low || count > 0) <$> mem_response_transfer <*> mem_outstanding
  mem_outstanding_next = (\count requestTransfer responseTransfer -> case (requestTransfer == high, responseTransfer == high) of { (True, False) -> if count < (2 :: Unsigned 2) then count + 1 else count; (False, True) -> if count > 0 then count - 1 else count; _ -> count }) <$> mem_outstanding <*> mem_request_transfer <*> mem_response_transfer
  (mem_ids_valid_next, mem_ids_next) = unbundle (zlangUpdateIds <$> mem_request_transfer <*> mem_request_id <*> mem_response_transfer <*> mem_response_id <*> mem_ids_valid <*> mem_ids)
  mem_id_ok = (\duplicate missing -> not duplicate && not missing) <$> mem_duplicate <*> mem_missing
  mem_request_valid_checked = Verification.checkI "mem_ids_valid" Verification.AutoRenderAs (Verification.assert mem_id_ok) . Verification.checkI "mem_within_limit" Verification.AutoRenderAs (Verification.assert mem_within_limit) $ mem_request_valid
  mem_response_ready_checked = Verification.checkI "mem_has_request" Verification.AutoRenderAs (Verification.assert mem_has_request) $ mem_response_ready

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> Signal ZLangSystem (Request) -> Signal ZLangSystem (Bit) -> Signal ZLangSystem (Bit) -> Signal ZLangSystem ZLangReadyValidBackward -> Signal ZLangSystem (ZLangReadyValidForward (Response)) -> (Signal ZLangSystem (Response), Signal ZLangSystem (ZLangReadyValidForward (Request)), Signal ZLangSystem ZLangReadyValidBackward)
topEntity clk rst request_payload issue accept_response mem_request_backward mem_response_forward = exposeClockResetEnable circuit clk rst enableGen request_payload issue accept_response mem_request_backward mem_response_forward

{-# ANN topEntity
  (Synthesize
    { t_name = "RequestClient"
    , t_inputs = [PortName "clk", PortName "rst", PortProduct "request_payload" [PortName "id", PortName "data"], PortName "issue", PortName "accept_response", PortName "mem_request_ready", PortProduct "mem_response" [PortProduct "payload" [PortName "id", PortName "data"], PortName "valid"]]
    , t_output = PortProduct "" [PortProduct "response_payload" [PortName "id", PortName "data"], PortProduct "mem_request" [PortProduct "payload" [PortName "id", PortName "data"], PortName "valid"], PortName "mem_response_ready"]
    }) #-}
