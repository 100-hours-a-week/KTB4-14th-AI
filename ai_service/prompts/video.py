"""추억영상의 분위기와 장면별 카메라 연출을 구성한다."""
STYLE_PROMPTS = {
    'WARM': 'A heartfelt, uplifting travel film with living scenery, natural human movement and warm light.',
    'LIVELY': 'A bright, joyful first-person travel experience with active physical movement and fresh natural colors.',
    'CINEMATIC': 'An immersive cinematic return to the trip with deep three-dimensional parallax and lifelike physical action.',
}

KOREAN_REFERENCE_CONTEXT = (
    'The travel destination is always South Korea, and if the original photo does not show a face, a video will be created at our discretion. In this case, the face should be set as a typical Korean. As long as a particular foreign face is not seen, a Korean is Korean. '
    'Keep exactly the people visible in the reference photographs: preserve their original faces, '
    'clothes and relationships while allowing natural changes of pose and position. Keep unoccupied areas empty. '
    'Do not add a person sitting or standing beside the original subjects, foreign tourists, '
    'new companions, bystanders, portraits or crowds. Preserve the real Korean location and signage.'
    ' The discretionary Korean face rule applies ONLY to an already visible person whose face is hidden. '
    'It never permits creating an additional person. Scenery-only references must stay scenery-only, '
    'with no people, no faces and no posed group portraits. The first-person camera operator stays off screen.'
)


def memory_prompt(request, plan, index: int) -> str:
    camera = ('A slow graceful camera push with natural foreground parallax.' if index % 2 == 0
              else 'A gentle lateral camera glide revealing the scene naturally.')
    transition = (
        'Create a motivated camera reveal between the two photographs while respecting both endpoint compositions. '
        'Both reference frames are mandatory visual anchors. Show only content visible in these references. '
        'Do not add new people, new objects or unrelated scenes. '
        'Preserve recognizable people, objects and architecture. Do not dissolve, crossfade, use double exposure '
        'or liquify faces and objects. Avoid abrupt scene changes and invented text.'
        if plan.kind == 'transition' else
        'Keep the same scene and subject. Add only subtle lifelike motion and a restrained camera drift. '
        'Preserve faces and hands. No dissolves, double exposure or scene morphing.'
    )
    context = f' Memory context: {request.memory_context}.' if request.memory_context else ''
    return f'{STYLE_PROMPTS[request.memory_style]} {KOREAN_REFERENCE_CONTEXT} {camera} {transition} {request.prompt}{context}'
