#!/bin/bash

git s-p onedusk;
git add -A;
git commit -m "first commit";
git branch -M main;
git remote add origin git@github.com:onedusk/cce.git;
git push -u origin main;
