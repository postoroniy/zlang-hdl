{-# LANGUAGE DataKinds #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}

module MacChoice where

import Clash.Prelude

createDomain vSystem{vName="ZLangSystem", vResetKind=Synchronous}

circuit :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 16) -> Signal ZLangSystem (Unsigned 17)
circuit a b c = y
 where
  pipeline_1_s1 = register (0 :: Unsigned 17) ((\value_0 value_1 value_2 -> (resize ((resize (value_0) :: Unsigned 16) * (resize (value_1) :: Unsigned 16)) :: Unsigned 17) + (resize (value_2) :: Unsigned 17)) <$> a <*> b <*> c)
  y = (\value_0 -> (let zlangDspMac = (value_0) in zlangDspMac)) <$> pipeline_1_s1

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 16) -> Signal ZLangSystem (Unsigned 17)
topEntity clk rst a b c = exposeClockResetEnable circuit clk rst enableGen a b c

{-# ANN topEntity
  (Synthesize
    { t_name = "MacChoice"
    , t_inputs = [PortName "clk", PortName "rst", PortName "a", PortName "b", PortName "c"]
    , t_output = PortName "y"
    }) #-}
