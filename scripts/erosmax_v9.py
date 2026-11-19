"""Build the normal two-pass API prompt from the bundled ErosMax v9 editor graph."""

import secrets

NAME = "H3_Turbo_2Pass_ErosMax_b7_advanced_refmods_v9"
GRAPH_ID = "5e120bdd-ac66-4a5f-a19a-1d5d27374342"


def build_prompt(graph, images, mode, args, aspect_ratio):
    # v9 uses distinct node IDs across the root and its subgraphs. Keep direct
    # links and widget settings from the export. Reconnect subgraph boundaries
    # and fold the normal/full-resolution branches below.
    prompt = {}
    sources = {}
    for group in [graph, *graph["definitions"]["subgraphs"]]:
        if group is graph:
            links = {link[0]: link[1:3] for link in group["links"]}
        else:
            links = {link["id"]: (link["origin_id"], link["origin_slot"])
                     for link in group["links"]}
        for node in group["nodes"]:
            sources[node["id"]] = node, links

    def add(node_id, *, api_id=None, **overrides):
        node, links = sources[node_id]
        values = list(node.get("widgets_values") or [])
        if node_id == 182:
            values.pop(4)  # UI-only seed control after scramble_seed
        widgets = [slot["name"] for slot in node["inputs"] if "widget" in slot]
        if node_id == 135:
            # This widget is serialized but has no socket in the editor graph.
            widgets.insert(1, "upscale_method")
        inputs = dict(zip(widgets, values))
        for slot in node["inputs"]:
            if slot.get("link") is not None:
                origin, index = links[slot["link"]]
                if origin != -10:  # subgraph input supplied below
                    inputs[slot["name"]] = [str(origin), index]
        inputs.update(overrides)
        key = api_id or str(node_id)
        prompt[key] = {"class_type": node["type"], "inputs": inputs}
        return [key, 0]

    add(2, value=args.prompt)
    add(3)
    add(4)
    add(15)
    add(17, value=args.duration)
    add(20, aspect_ratio=aspect_ratio)
    add(23, value=args.megapixels)
    add(30)
    add(38, model=["30", 0])  # all optional LoRA rows are disabled in v9
    add(100)
    add(37)
    add(18)
    add(31, noise_seed=secrets.randbits(50) if args.seed in (None, -1) else args.seed)
    add(24, **({"steps": args.steps} if args.steps is not None else {}))
    add(102)
    add(19, **{"values.a": ["17", 0]})
    add(12, preview_frames=["19", 1])
    for node_id in (195, 196):
        add(node_id, **{"values.a": ["20", 0], "values.b": ["20", 1], "values.c": ["23", 0]})

    add(11, clip=["15", 0], vae=["3", 0], audio_vae=["4", 0], prompt=["2", 0],
        width=["20", 0], height=["20", 1], ref_image_size=args.ref_image_size)
    references = prompt["11"]["inputs"]
    del references["ref_images.ref_image_7"]  # inactive continuation image
    add(181)
    add(182, mods=["181", 0])
    first_conditioning = second_conditioning = ["182", 0]
    for index, image in enumerate(images):
        loader_id = f"image_{index}"
        prompt[loader_id] = {"class_type": "LoadImage", "inputs": {"image": image}}
        image_link = [loader_id, 0]
        if mode != "ref2va":
            image_link = add(198, api_id=f"crop_{index}", image=image_link,
                             width=["195", 1], height=["196", 1])
            # Anchor each requested frame in the coordinate space of both passes.
            frame_idx = 0 if index == 0 else -1
            first_conditioning = add(186, api_id=f"186_{index}", positive=first_conditioning,
                                     latent=["11", 1], image=image_link, frame_idx=frame_idx)
            second_conditioning = add(187, api_id=f"187_{index}", positive=second_conditioning,
                                      latent=["10", 0], image=image_link, frame_idx=frame_idx)
        references[f"ref_images.ref_image_{index}"] = image_link

    add(29, model=["12", 0], conditioning=first_conditioning)
    add(21, noise=["31", 0], sampler=["18", 0], sigmas=["24", 0])
    add(35)
    add(199)
    add(10, video_latent=["199", 0])
    add(32, model=["37", 0], conditioning=second_conditioning)
    add(16, noise=["31", 0], sampler=["18", 0], sigmas=["102", 0])
    add(25, samples=["16", 0], vae=["3", 0])
    add(39, images=["25", 0])
    add(86, any_input=["35", 1], image_pass=["39", 0])
    add(14, vae=["4", 0])
    grain = sources[174][0]["widgets_values"][0]
    preset = sources[178][0]["widgets_values"][0]
    add(135, images=["39", 0], original_fps=24, grain=grain > 0,
        grain_intensity=grain, encoder_preset=preset)
    add(137, complete_audio=["14", 0])
    add(40, video=["137", 0], filename=sources[73][0]["widgets_values"][0])
    return prompt
