| gate | T | all | canvas only |
|---|---|---|---|
| conf | 0.6 | e2e 0.885, present 148/177, absent rejected 153/163, false taps 10 | e2e 0.906, present 67/74, absent rejected 48/53, false taps 5 |
| conf | 0.7 | e2e 0.859, present 137/177, absent rejected 155/163, false taps 8 | e2e 0.882, present 63/74, absent rejected 49/53, false taps 4 |
| vis | 0.5 | e2e 0.974, present 169/177, absent rejected 162/163, false taps 6 | e2e 0.961, present 69/74, absent rejected 53/53, false taps 5 |
| both | 0.3 | e2e 0.971, present 168/177, absent rejected 162/163, false taps 4 | e2e 0.961, present 69/74, absent rejected 53/53, false taps 3 |
| both | 0.5 | e2e 0.935, present 156/177, absent rejected 162/163, false taps 1 | e2e 0.945, present 67/74, absent rejected 53/53, false taps 0 |
| vis+conf>=0.3 | 0.5 | e2e 0.971, present 168/177, absent rejected 162/163, false taps 4 | e2e 0.961, present 69/74, absent rejected 53/53, false taps 3 |

Canvas targets where Jev Box or Haiku missed:
- canvas_game_menu_icons_1 the Settings button: Jev hit=False (conf 0.40, visible 1.0), Haiku hit=True
- canvas_game_menu_icons_3 the Settings button: Jev hit=False (conf 0.26, visible 1.0), Haiku hit=True
- canvas_game_over_1 the Menu button: Jev hit=False (conf 0.46, visible 0.98), Haiku hit=True
- canvas_game_over_2 the Menu button: Jev hit=False (conf 0.39, visible 0.99), Haiku hit=True
- canvas_game_over_5 the Menu button: Jev hit=False (conf 0.26, visible 0.97), Haiku hit=True
