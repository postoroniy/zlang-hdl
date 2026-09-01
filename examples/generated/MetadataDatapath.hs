{-# LANGUAGE DataKinds #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}

module MetadataDatapath where

import Clash.Prelude

createDomain vSystem{vName="ZLangSystem", vResetKind=Synchronous}

circuit :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem (Vec 4 (Unsigned 8)) -> Signal ZLangSystem (Vec 4 (Unsigned 8)) -> Signal ZLangSystem (Unsigned 18)
circuit samples coefficients = y
 where
  delay_0_s1 = register (0 :: Unsigned 18) ((\value_0 value_1 -> (resize ((resize ((resize ((value_0) !! (0 :: Index 4)) :: Unsigned 16) * (resize ((value_1) !! (0 :: Index 4)) :: Unsigned 16)) :: Unsigned 17) + (resize ((resize ((value_0) !! (1 :: Index 4)) :: Unsigned 16) * (resize ((value_1) !! (1 :: Index 4)) :: Unsigned 16)) :: Unsigned 17)) :: Unsigned 18) + (resize ((resize ((resize ((value_0) !! (2 :: Index 4)) :: Unsigned 16) * (resize ((value_1) !! (2 :: Index 4)) :: Unsigned 16)) :: Unsigned 17) + (resize ((resize ((value_0) !! (3 :: Index 4)) :: Unsigned 16) * (resize ((value_1) !! (3 :: Index 4)) :: Unsigned 16)) :: Unsigned 17)) :: Unsigned 18)) <$> samples <*> coefficients)
  y = delay_0_s1

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> Signal ZLangSystem (Vec 4 (Unsigned 8)) -> Signal ZLangSystem (Vec 4 (Unsigned 8)) -> Signal ZLangSystem (Unsigned 18)
topEntity clk rst samples coefficients = exposeClockResetEnable circuit clk rst enableGen samples coefficients

{-# ANN topEntity
  (Synthesize
    { t_name = "MetadataDatapath"
    , t_inputs = [PortName "clk", PortName "rst", PortName "samples", PortName "coefficients"]
    , t_output = PortName "y"
    }) #-}
