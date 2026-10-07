# Menus and dialogs drawn inside the editor window (the Linux editor; see PlatformUiDrawn.h).
add_library(sq8l_gui_drawn STATIC ${CMAKE_CURRENT_SOURCE_DIR}/PlatformUiDrawn.cpp)
target_include_directories(sq8l_gui_drawn PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/..)
target_link_libraries(sq8l_gui_drawn PUBLIC sq8l_gui_logic sq8l_gui)
target_compile_options(sq8l_gui_drawn PRIVATE ${SQ8L_WARN_FLAGS})
