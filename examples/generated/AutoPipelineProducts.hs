{-# LANGUAGE DataKinds #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}

module AutoPipelineProducts where

import Clash.Prelude

createDomain vSystem{vName="ZLangSystem", vResetKind=Synchronous}

circuit :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 19)
circuit a b c d e f g h = y
 where
  y = (\value_0 value_1 value_2 value_3 value_4 value_5 value_6 value_7 -> (resize ((resize ((resize ((resize (value_0) :: Unsigned 16) * (resize (value_1) :: Unsigned 16)) :: Unsigned 19)) :: Unsigned 19) + (resize ((resize ((resize (value_2) :: Unsigned 16) * (resize (value_3) :: Unsigned 16)) :: Unsigned 19)) :: Unsigned 19)) :: Unsigned 19) + (resize ((resize ((resize ((resize (value_4) :: Unsigned 16) * (resize (value_5) :: Unsigned 16)) :: Unsigned 19)) :: Unsigned 19) + (resize ((resize ((resize (value_6) :: Unsigned 16) * (resize (value_7) :: Unsigned 16)) :: Unsigned 19)) :: Unsigned 19)) :: Unsigned 19)) <$> a <*> b <*> c <*> d <*> e <*> f <*> g <*> h

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 19)
topEntity clk rst a b c d e f g h = exposeClockResetEnable circuit clk rst enableGen a b c d e f g h

{-# ANN topEntity
  (Synthesize
    { t_name = "AutoPipelineProducts"
    , t_inputs = [PortName "clk", PortName "rst", PortName "a", PortName "b", PortName "c", PortName "d", PortName "e", PortName "f", PortName "g", PortName "h"]
    , t_output = PortName "y"
    }) #-}
