{-# LANGUAGE DataKinds #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}

module DelayedMul where

import Clash.Prelude

createDomain vSystem{vName="ZLangSystem", vResetKind=Synchronous}

circuit :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 16)
circuit a b = y
 where
  delay_0_s1 = register (0 :: Unsigned 16) ((\value_0 value_1 -> (resize (value_0) :: Unsigned 16) * (resize (value_1) :: Unsigned 16)) <$> a <*> b)
  delay_0_s2 = register (0 :: Unsigned 16) (delay_0_s1)
  y = delay_0_s2

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 16)
topEntity clk rst a b = exposeClockResetEnable circuit clk rst enableGen a b

{-# ANN topEntity
  (Synthesize
    { t_name = "DelayedMul"
    , t_inputs = [PortName "clk", PortName "rst", PortName "a", PortName "b"]
    , t_output = PortName "y"
    }) #-}
