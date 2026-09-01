{-# LANGUAGE DataKinds #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}

module RuleCounter where

import Clash.Prelude

createDomain vSystem{vName="ZLangSystem", vResetKind=Synchronous}

circuit :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem (Bit) -> Signal ZLangSystem (Bit) -> Signal ZLangSystem (Unsigned 8)
circuit increment clear = count_out
 where
  reset_active = unsafeToActiveHigh hasReset
  rule_clear_count_fire = (\guard resetActive -> if resetActive then low else guard) <$> clear <*> reset_active
  rule_increment_count_fire = (\guard resetActive blocked0 -> if not resetActive && guard == high && blocked0 == low then high else low) <$> increment <*> reset_active <*> rule_clear_count_fire
  count = register (0 :: Unsigned 8) (count_next)
  count_next = (\value_0 value_1 value_2 -> if (value_0) == high then ((0 :: Unsigned 8)) else (if (value_1) == high then ((resize ((resize (value_2) :: Unsigned 9) + (resize ((1 :: Unsigned 8)) :: Unsigned 9)) :: Unsigned 8)) else (value_2))) <$> rule_clear_count_fire <*> rule_increment_count_fire <*> count
  count_out = count

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> Signal ZLangSystem (Bit) -> Signal ZLangSystem (Bit) -> Signal ZLangSystem (Unsigned 8)
topEntity clk rst increment clear = exposeClockResetEnable circuit clk rst enableGen increment clear

{-# ANN topEntity
  (Synthesize
    { t_name = "RuleCounter"
    , t_inputs = [PortName "clk", PortName "rst", PortName "increment", PortName "clear"]
    , t_output = PortName "count_out"
    }) #-}
