{-# LANGUAGE DataKinds #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}

module AutoPipelineProducts where

import Clash.Prelude

-- ZLang pipeline(auto): output=y selected=balanced_levels_dsp tree=balanced registers=balanced_levels multipliers=dsp latency=3 ii=1
createDomain vSystem{vName="ZLangSystem", vResetKind=Synchronous}

circuit :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 19)
circuit a b c d e f g h = y
 where
  zlang_pipe_10_s1 = register (0 :: Unsigned 16) ((\value_0 value_1 -> (resize (value_0) :: Unsigned 16) * (resize (value_1) :: Unsigned 16)) <$> a <*> b)
  zlang_pipe_11_s1 = register (0 :: Unsigned 16) ((\value_0 value_1 -> (resize (value_0) :: Unsigned 16) * (resize (value_1) :: Unsigned 16)) <$> c <*> d)
  zlang_pipe_14_s1 = register (0 :: Unsigned 17) ((\value_0 value_1 -> (resize (value_0) :: Unsigned 17) + (resize (value_1) :: Unsigned 17)) <$> zlang_pipe_10_s1 <*> zlang_pipe_11_s1)
  zlang_pipe_12_s1 = register (0 :: Unsigned 16) ((\value_0 value_1 -> (resize (value_0) :: Unsigned 16) * (resize (value_1) :: Unsigned 16)) <$> e <*> f)
  zlang_pipe_13_s1 = register (0 :: Unsigned 16) ((\value_0 value_1 -> (resize (value_0) :: Unsigned 16) * (resize (value_1) :: Unsigned 16)) <$> g <*> h)
  zlang_pipe_15_s1 = register (0 :: Unsigned 17) ((\value_0 value_1 -> (resize (value_0) :: Unsigned 17) + (resize (value_1) :: Unsigned 17)) <$> zlang_pipe_12_s1 <*> zlang_pipe_13_s1)
  zlang_pipe_16_s1 = register (0 :: Unsigned 18) ((\value_0 value_1 -> (resize (value_0) :: Unsigned 18) + (resize (value_1) :: Unsigned 18)) <$> zlang_pipe_14_s1 <*> zlang_pipe_15_s1)
  y = (\value_0 -> (resize (value_0) :: Unsigned 19)) <$> zlang_pipe_16_s1

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 19)
topEntity clk rst a b c d e f g h = exposeClockResetEnable circuit clk rst enableGen a b c d e f g h

{-# ANN topEntity
  (Synthesize
    { t_name = "AutoPipelineProducts"
    , t_inputs = [PortName "clk", PortName "rst", PortName "a", PortName "b", PortName "c", PortName "d", PortName "e", PortName "f", PortName "g", PortName "h"]
    , t_output = PortName "y"
    }) #-}
