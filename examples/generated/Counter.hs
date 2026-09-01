{-# LANGUAGE DataKinds #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}

module Counter where

import Clash.Prelude

createDomain vSystem{vName="ZLangSystem", vResetKind=Synchronous}

circuit :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem (Unsigned 8)
circuit = y
 where
  count = register (0 :: Unsigned 8) (count_next)
  count_next = (\value_0 -> (resize ((resize (value_0) :: Unsigned 9) + (resize ((1 :: Unsigned 8)) :: Unsigned 9)) :: Unsigned 8)) <$> count
  y = count

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> Signal ZLangSystem (Unsigned 8)
topEntity clk rst = exposeClockResetEnable circuit clk rst enableGen

{-# ANN topEntity
  (Synthesize
    { t_name = "Counter"
    , t_inputs = [PortName "clk", PortName "rst"]
    , t_output = PortName "y"
    }) #-}
