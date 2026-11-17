% NLCL pretrained knowledge (training split only) - written 2026-11-16 18:26:25
% (plain Prolog facts: edit, delete or load them in SWI-Prolog)

% rewrite/3
rewrite(['store\'s'],[store],1).
rewrite([get,rid,of],[delete],1).
rewrite([the,game],[],1).

% reliability/3
reliability(frame(behavior),18,1).
reliability(frame(connect_to),2,0).
reliability(frame(create_object),22,4).
reliability(frame(delete_object),5,0).
reliability(frame(move_object),0,1).
reliability(frame(rename_object),2,0).
reliability(frame(set_property),12,13).
reliability(frame(special),11,0).
reliability(intent(archive),3,0).
reliability(intent(copy),3,0).
reliability(intent(count_files),3,0).
reliability(intent(count_lines),3,0).
reliability(intent(delete),3,0).
reliability(intent(list_files),3,0).
reliability(intent(make_dir),3,0).
reliability(intent(move),3,0).
reliability(intent(replace_text),3,0).
reliability(intent(search_text),3,0).
reliability(intent(show_file),3,0).

% param/2
param(bias,-1.11072).
param(coverage,2.257904).
param(dry_errors,-2.4).
param(errors,-2.2).
param(frame_rel,0.858535).
param(fuzzy,-0.9).
param(inferred,-0.404714).
param(llm,0.4).
param(missing_paths,-1.8).
param(n_ops,-0.292096).
param(notes,-0.65517).
param(novel,-1.853353).
param(question,-0.6).
param(reliability,0.957997).
param(shell,1.213662).
param(unknown,-0.9).
param(verbless,-1.796907).
param(warnings,-0.25).

