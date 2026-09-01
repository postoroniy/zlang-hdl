{-# LANGUAGE DataKinds #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}

module ContractedAdd where

import Clash.Prelude

createDomain vSystem{vName="ZLangSystem", vResetKind=Synchronous}

circuit :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem (Unsigned 4) -> Signal ZLangSystem (Unsigned 4) -> Signal ZLangSystem (Unsigned 5)
circuit a b = y
 where
  y = (\value_0 value_1 -> (resize (value_0) :: Unsigned 5) + (resize (value_1) :: Unsigned 5)) <$> a <*> b

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> Signal ZLangSystem (Unsigned 4) -> Signal ZLangSystem (Unsigned 4) -> Signal ZLangSystem (Unsigned 5)
topEntity clk rst a b = exposeClockResetEnable circuit clk rst enableGen a b

{-# ANN topEntity
  (Synthesize
    { t_name = "ContractedAdd"
    , t_inputs = [PortName "clk", PortName "rst", PortName "a", PortName "b"]
    , t_output = PortName "y"
    }) #-}
